"""Flask app factory and routes for the KFC Entra User Manager."""
from __future__ import annotations

from flask import (
    Blueprint,
    Flask,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
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
from .graph_client import GraphClient, GraphError
from .users import invite_and_promote, list_users_sorted

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
    client = GraphClient(get_access_token())
    try:
        users = list_users_sorted(client, search=search)
        return render_template(
            "users_list.html", users=users, user=current_user(), search=search or ""
        )
    except GraphError as exc:
        flash(f"Failed to load users: {exc.message}", "error")
        return render_template(
            "users_list.html", users=[], user=current_user(), search=search or ""
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
