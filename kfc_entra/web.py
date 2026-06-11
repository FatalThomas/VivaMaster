"""Flask app factory and routes for the KFC Entra User Manager."""
from __future__ import annotations

import csv
import io
import json
import re
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
    get_access_token,
    login_required,
    open_in_system_browser,
    poll_device_flow,
    start_device_flow,
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
from .updates import (
    download_update,
    get_available_update,
    install_update_and_restart,
    self_install_supported,
)
from .users import invite_and_promote, list_users_sorted
from .version import __version__

# Parsed uploads waiting for the apply step. This app is a single-process
# local tool, so in-memory is fine; we keep only the most recent few.
# In-memory mirror of the upload store, plus an on-disk copy in the per-user
# config dir so closing the app no longer loses your parsed CSV.
_UPLOAD_STORE: OrderedDict[str, list[dict]] = OrderedDict()
_UPLOAD_STORE_MAX = 5


def _uploads_dir():
    from .mappings import config_dir
    d = config_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trim_uploads_dir() -> None:
    """Keep only the most recent _UPLOAD_STORE_MAX upload files on disk."""
    try:
        files = sorted(
            _uploads_dir().glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[_UPLOAD_STORE_MAX:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _stash_upload(rows: list[EmployeeRow]) -> str:
    token = uuid.uuid4().hex
    payload = [r.to_dict() for r in rows]
    _UPLOAD_STORE[token] = payload
    while len(_UPLOAD_STORE) > _UPLOAD_STORE_MAX:
        _UPLOAD_STORE.popitem(last=False)
    # Persist to disk too - a 52k-row report takes ~30 MB of JSON which is
    # cheap, and surviving an app restart is worth a lot of UX.
    try:
        with open(_uploads_dir() / f"{token}.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        _trim_uploads_dir()
    except OSError:
        pass  # disk full / permissions - in-memory copy still works
    return token


def _load_upload(token: str) -> list[EmployeeRow] | None:
    raw = _UPLOAD_STORE.get(token or "")
    if raw is None and token:
        try:
            with open(_uploads_dir() / f"{token}.json", encoding="utf-8") as fh:
                raw = json.load(fh)
            _UPLOAD_STORE[token] = raw  # warm the cache
        except (OSError, ValueError):
            raw = None
    if raw is None:
        return None
    return [EmployeeRow.from_dict(d) for d in raw]


def _sse_response(events) -> Response:
    """Wrap an event iterator in an SSE response with periodic heartbeats.

    Long bulk operations can run for many minutes - SSE comment lines
    (":heartbeat\\n\\n") sent every 15s keep the TCP connection from being
    killed by idle timeouts (corporate proxies, WebView2 default fetch
    behaviour) without affecting the client's JSON event parsing.
    """
    import time as _time

    HEARTBEAT_SECONDS = 15

    def generate():
        last_emit = _time.monotonic()
        for event in events:
            yield f"data: {json.dumps(event)}\n\n"
            now = _time.monotonic()
            if now - last_emit > HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
            last_emit = now

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
    """Render the device-code sign-in page.

    Already-signed-in users skip straight to the dashboard so they can't
    accidentally start a second device flow on top of a valid session.
    """
    if current_user() and get_access_token():
        return redirect(url_for("main.dashboard"))
    try:
        info = start_device_flow()
    except RuntimeError as exc:
        flash(f"Could not start sign-in: {exc}", "error")
        return redirect(url_for("main.landing"))
    return render_template("device_login.html", info=info)


@auth_bp.route("/login/status")
def login_status():
    """Polled by the device-code page until sign-in completes."""
    return jsonify(poll_device_flow())


@auth_bp.route("/login/open-browser", methods=["POST"])
def login_open_browser():
    """Open the Microsoft devicelogin page in the user's default browser.

    Restricted to known Microsoft hosts in auth.py so a stray local
    request can't launch arbitrary URLs.
    """
    payload = request.get_json(silent=True) or {}
    opened = open_in_system_browser(payload.get("url", ""))
    return jsonify({"opened": opened})


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
        if search:
            # Search path: $search returns the first matching page (Graph
            # caps it). The type filter is then applied client-side on
            # whatever came back - best effort for partial matches.
            users = list_users_sorted(client, search=search)
            if type_filter == "members":
                users = [u for u in users if u.user_type == "Member"]
            elif type_filter == "guests":
                users = [u for u in users if u.user_type == "Guest"]
        else:
            # Browse path: push the userType filter to Graph and walk every
            # page so the listing reflects the *whole* tenant, not just the
            # first 100 names alphabetically. This is what was breaking
            # "Guests only" - the unpaginated 100-user fetch could easily
            # contain zero guests in a large tenant.
            wanted = {"members": "Member", "guests": "Guest"}.get(type_filter)
            users = client.list_users_by_type(wanted)
            users.sort(key=lambda u: (u.display_name or "").lower())
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


# ---------- self-update ----------
# No @login_required: this touches the local exe, not Microsoft Graph, and
# must work from the sign-in page too. The app only listens on 127.0.0.1.
@main_bp.route("/updates/install", methods=["POST"])
def install_update():
    info = get_available_update()
    if not info:
        return jsonify({"error": "No update available."}), 400
    if not self_install_supported():
        return (
            jsonify(
                {
                    "error": "Self-update only works in the packaged exe. "
                    "Running from source? Use git pull instead.",
                    "release_url": info.release_url,
                }
            ),
            400,
        )
    try:
        new_exe = download_update(info)
    except Exception as exc:  # surface download errors verbatim to the UI
        return jsonify({"error": str(exc)}), 502
    install_update_and_restart(new_exe)
    return jsonify({"status": "restarting", "version": info.latest_version})


# ---------- generic CSV download ----------
# pywebview's WebView2 backend silently blocks JS-initiated Blob URL downloads
# (`<a download>` clicks created in script), so the failure-CSV button on
# bulk operations never produced a file. This endpoint generates the CSV
# server-side and returns it as a real attachment - the browser handles it
# natively, which works in WebView2 too.
@main_bp.route("/downloads/csv", methods=["POST"])
@login_required
def download_csv():
    raw = request.form.get("payload") or ""
    try:
        payload = json.loads(raw) if raw else (request.get_json(silent=True) or {})
    except (ValueError, TypeError):
        payload = {}

    filename = (payload.get("filename") or "download.csv").strip()
    filename = re.sub(r"[^\w\-. ]", "", filename) or "download.csv"
    if not filename.lower().endswith(".csv"):
        filename = filename + ".csv"

    headers_row = payload.get("headers") or []
    rows = payload.get("rows") or []

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    if headers_row:
        writer.writerow(headers_row)
    for row in rows:
        writer.writerow([(v if v is not None else "") for v in row])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


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
        return {
            "current_user": current_user(),
            "app_version": __version__,
            "available_update": get_available_update(),
            "self_install_supported": self_install_supported(),
        }

    return app
