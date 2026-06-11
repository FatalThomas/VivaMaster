"""Flask app factory and routes for the KFC Entra User Manager."""
from __future__ import annotations

import json
import uuid
from collections import OrderedDict

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

from config import load_config

from .auth import (
    clear_session,
    current_user,
    finish_auth_flow,
    get_access_token,
    login_required,
    start_auth_flow,
)
from .bulk import iter_apply_mappings, iter_convert_to_member
from .graph_client import GraphClient, GraphError
from .mappings import delete_mapping, load_mappings, save_mapping
from .report import (
    EmployeeRow,
    ReportParseError,
    group_by_franchisee,
    parse_report,
)
from .users import invite_and_promote, list_users_sorted

# Parsed uploads waiting for the apply step. This app is a single-process
# local tool, so in-memory is fine; we keep only the most recent few.
_UPLOAD_STORE: OrderedDict[str, list[dict]] = OrderedDict()
_UPLOAD_STORE_MAX = 5


def _stash_upload(rows: list[EmployeeRow]) -> str:
    token = uuid.uuid4().hex
    _UPLOAD_STORE[token] = [r.to_dict() for r in rows]
    while len(_UPLOAD_STORE) > _UPLOAD_STORE_MAX:
        _UPLOAD_STORE.popitem(last=False)
    return token


def _load_upload(token: str) -> list[EmployeeRow] | None:
    raw = _UPLOAD_STORE.get(token or "")
    if raw is None:
        return None
    return [EmployeeRow.from_dict(d) for d in raw]


def _sse_response(events) -> Response:
    def generate():
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

auth_bp = Blueprint("auth", __name__)
main_bp = Blueprint("main", __name__)


# ---------- auth routes ----------
@auth_bp.route("/login")
def login():
    return redirect(start_auth_flow())


@auth_bp.route("/auth/callback")
def auth_callback():
    result = finish_auth_flow(request.args.to_dict())
    if "error" in result:
        flash(
            f"Sign-in failed: {result.get('error_description') or result.get('error')}",
            "error",
        )
        return redirect(url_for("auth.login"))
    next_url = session.pop("post_login_redirect", None) or url_for("main.dashboard")
    return redirect(next_url)


@auth_bp.route("/logout")
def logout():
    clear_session()
    return redirect(url_for("main.landing"))


# ---------- main routes ----------
@main_bp.route("/")
def landing():
    if current_user() and get_access_token():
        return redirect(url_for("main.dashboard"))
    return render_template("login.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user())


@main_bp.route("/users")
@login_required
def users_list():
    search = (request.args.get("q") or "").strip() or None
    type_filter = (request.args.get("type") or "all").lower()
    if type_filter not in ("all", "members", "guests"):
        type_filter = "all"
    client = GraphClient(get_access_token())
    try:
        users = list_users_sorted(client, search=search)
        if type_filter == "members":
            users = [u for u in users if u.user_type == "Member"]
        elif type_filter == "guests":
            users = [u for u in users if u.user_type == "Guest"]
        return render_template(
            "users_list.html",
            users=users,
            user=current_user(),
            search=search or "",
            type_filter=type_filter,
        )
    except GraphError as exc:
        flash(f"Failed to load users: {exc.message}", "error")
        return render_template(
            "users_list.html",
            users=[],
            user=current_user(),
            search=search or "",
            type_filter=type_filter,
        )


@main_bp.route("/users/add", methods=["GET", "POST"])
@login_required
def add_user():
    if request.method == "GET":
        return render_template("add_user.html", user=current_user(), form={})

    form = {
        "email": (request.form.get("email") or "").strip(),
        "display_name": (request.form.get("display_name") or "").strip(),
        "send_invitation_message": request.form.get("send_invitation_message") == "on",
    }

    if not form["email"] or "@" not in form["email"]:
        flash("A valid email address is required.", "error")
        return render_template("add_user.html", user=current_user(), form=form)

    display_name = form["display_name"] or _display_name_from_email(form["email"])

    cfg = current_app.config["KFC_CONFIG"]
    client = GraphClient(get_access_token())
    try:
        result = invite_and_promote(
            client=client,
            email=form["email"],
            display_name=display_name,
            invite_redirect_url=cfg.invite_redirect_url,
            send_invitation_message=form["send_invitation_message"],
        )
    except GraphError as exc:
        flash(f"Failed to invite user: {exc.message}", "error")
        return render_template("add_user.html", user=current_user(), form=form)

    if result.converted_to_member:
        flash(
            f"Invited {result.email} as '{result.display_name}' and set them as Member.",
            "success",
        )
    else:
        flash(result.warning or "Invitation sent, but conversion to Member failed.", "warning")
    return redirect(url_for("main.users_list"))


@main_bp.route("/users/<user_id>/convert-to-member", methods=["POST"])
@login_required
def convert_to_member(user_id: str):
    client = GraphClient(get_access_token())
    try:
        client.convert_to_member(user_id)
        flash("User converted to Member.", "success")
    except GraphError as exc:
        flash(f"Failed to convert user: {exc.message}", "error")
    return redirect(url_for("main.users_list"))


@main_bp.route("/users/<user_id>/display-name", methods=["POST"])
@login_required
def update_display_name(user_id: str):
    new_name = (request.form.get("display_name") or "").strip()
    if not new_name:
        flash("Display name cannot be empty.", "error")
        return redirect(url_for("main.users_list"))
    client = GraphClient(get_access_token())
    try:
        client.set_display_name(user_id, new_name)
        flash(f"Display name updated to '{new_name}'.", "success")
    except GraphError as exc:
        flash(f"Failed to update display name: {exc.message}", "error")
    return redirect(url_for("main.users_list"))


# ---------- bulk convert (SSE) ----------
@main_bp.route("/users/convert/confirm", methods=["POST"])
@login_required
def convert_confirm():
    """Return how many users a bulk convert would touch, for the modal."""
    payload = request.get_json(silent=True) or {}
    scope = (payload.get("scope") or request.args.get("scope") or "guests").lower()
    user_ids = payload.get("user_ids") or []
    client = GraphClient(get_access_token())
    try:
        if user_ids:
            count = len(user_ids)
            scope = "selected"
        elif scope == "all":
            count = sum(1 for u in client.list_users_by_type(None) if u.user_type != "Member")
        else:
            scope = "guests"
            count = len(client.list_users_by_type("Guest"))
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({"scope": scope, "count": count})


@main_bp.route("/users/convert/stream", methods=["GET", "POST"])
@login_required
def convert_stream():
    """SSE stream that converts users to Member.

    GET  ?scope=guests|all          - bulk by user type
    POST {"user_ids": [...]}        - explicit selection from the Users table
    """
    scope = (request.args.get("scope") or "guests").lower()
    user_ids: list[str] | None = None
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        ids = payload.get("user_ids") or []
        user_ids = [str(i) for i in ids if str(i).strip()] or None
        if user_ids:
            scope = "selected"
    if scope not in ("guests", "all", "selected"):
        scope = "guests"
    client = GraphClient(get_access_token())
    return _sse_response(iter_convert_to_member(client, scope=scope, user_ids=user_ids))


# ---------- employee report upload ----------
@main_bp.route("/report")
@login_required
def report_upload_page():
    return render_template("report_upload.html", user=current_user())


@main_bp.route("/report/upload", methods=["POST"])
@login_required
def report_upload():
    file = request.files.get("report")
    if file is None or not file.filename:
        flash("Choose a CSV, TSV, or XLSX employee report first.", "error")
        return redirect(url_for("main.report_upload_page"))

    data = file.read()
    try:
        rows = parse_report(data, file.filename)
    except ReportParseError as exc:
        flash(f"Could not parse '{file.filename}': {exc}", "error")
        return redirect(url_for("main.report_upload_page"))

    upload_id = _stash_upload(rows)
    franchisees = group_by_franchisee(rows)
    saved = load_mappings()["mappings"]

    groups_json: list[dict] = []
    groups_error = None
    client = GraphClient(get_access_token())
    try:
        groups_json = [
            {"id": g.id, "name": g.display_name} for g in client.list_groups()
        ]
    except GraphError as exc:
        groups_error = exc.message

    return render_template(
        "report_preview.html",
        user=current_user(),
        filename=file.filename,
        upload_id=upload_id,
        franchisees=franchisees,
        total_rows=len(rows),
        saved_mappings=saved,
        groups_json=groups_json,
        groups_error=groups_error,
    )


@main_bp.route("/report/apply/stream", methods=["POST"])
@login_required
def report_apply_stream():
    """SSE stream that applies Franchisee -> Group assignments to a parsed upload."""
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("upload_id") or ""
    rows = _load_upload(upload_id)
    if rows is None:
        return _sse_response(
            [{"type": "error",
              "message": "Upload expired or not found - please re-upload the report."}]
        )
    assignments = payload.get("assignments") or {}
    if not isinstance(assignments, dict):
        assignments = {}
    include_unknown = bool(payload.get("include_unknown"))
    client = GraphClient(get_access_token())
    return _sse_response(
        iter_apply_mappings(
            client, rows, assignments=assignments, include_unknown=include_unknown
        )
    )


# ---------- mappings management ----------
@main_bp.route("/report/mappings")
@login_required
def report_mappings():
    saved = load_mappings()["mappings"]
    groups_json: list[dict] = []
    groups_error = None
    client = GraphClient(get_access_token())
    try:
        groups_json = [
            {"id": g.id, "name": g.display_name} for g in client.list_groups()
        ]
    except GraphError as exc:
        groups_error = exc.message
    return render_template(
        "report_mappings.html",
        user=current_user(),
        mappings=saved,
        groups_json=groups_json,
        groups_error=groups_error,
    )


@main_bp.route("/report/mappings", methods=["POST"])
@login_required
def report_mappings_save():
    code = (request.form.get("code") or "").strip().upper()
    group_id = (request.form.get("group_id") or "").strip()
    group_name = (request.form.get("group_name") or "").strip()
    if not code:
        flash("A Franchisee code is required.", "error")
        return redirect(url_for("main.report_mappings"))
    if not group_id and not group_name:
        flash("Pick an existing group or type a new group name.", "error")
        return redirect(url_for("main.report_mappings"))
    if not group_id:
        # Creating the group now keeps the mapping store consistent: every
        # saved mapping always points at a real group id.
        client = GraphClient(get_access_token())
        try:
            group = client.create_group(group_name)
        except GraphError as exc:
            flash(f"Could not create group '{group_name}': {exc.message}", "error")
            return redirect(url_for("main.report_mappings"))
        group_id, group_name = group.id, group.display_name
        flash(f"Created new Entra group '{group_name}'.", "success")
    save_mapping(code, group_id, group_name)
    flash(f"Mapping saved: {code} → {group_name}.", "success")
    return redirect(url_for("main.report_mappings"))


@main_bp.route("/report/mappings/<code>/delete", methods=["POST"])
@login_required
def report_mappings_delete(code: str):
    if delete_mapping(code):
        flash(f"Mapping for {code.upper()} deleted.", "success")
    else:
        flash(f"No mapping found for {code.upper()}.", "warning")
    return redirect(url_for("main.report_mappings"))


def _display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    parts = [p for p in local.replace("_", ".").split(".") if p]
    return " ".join(p.capitalize() for p in parts) if parts else email


def create_app() -> Flask:
    cfg = load_config()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["KFC_CONFIG"] = cfg
    app.secret_key = cfg.flask_secret_key

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}

    return app
