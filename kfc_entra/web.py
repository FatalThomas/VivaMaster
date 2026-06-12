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
from .bulk import (
    iter_apply_mappings,
    iter_bulk_offboard,
    iter_convert_to_member,
    parse_offboard_emails,
)
from .graph_client import GraphClient, GraphError
from .mappings import (
    delete_mapping,
    delete_store_mapping,
    load_mappings,
    load_store_mappings,
    save_mapping,
    save_store_mapping,
)
from .report import (
    EmployeeRow,
    ReportParseError,
    group_by_franchisee,
    group_by_store,
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

# Per-session cache of the /users list, keyed by (sid, type_filter). First
# fetch costs the Graph round-trips; subsequent filter swaps within the TTL
# read straight from memory and feel instant. Refresh button busts it.
import time as _time

_USERS_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_USERS_CACHE_TTL = 90  # seconds


def _users_cache_get(sid: str, type_filter: str):
    entry = _USERS_CACHE.get((sid, type_filter))
    if not entry:
        return None
    cached_at, users = entry
    if _time.time() - cached_at > _USERS_CACHE_TTL:
        _USERS_CACHE.pop((sid, type_filter), None)
        return None
    return users


def _users_cache_put(sid: str, type_filter: str, users: list) -> None:
    _USERS_CACHE[(sid, type_filter)] = (_time.time(), users)


def _users_cache_clear(sid: str) -> None:
    for key in [k for k in _USERS_CACHE if k[0] == sid]:
        _USERS_CACHE.pop(key, None)


# Cached Entra group list + owner-count map for the Groups page. Same
# 90-second TTL pattern as users.
_GROUPS_CACHE: dict[str, tuple[float, list]] = {}
_OWNER_COUNTS_CACHE: dict[str, tuple[float, dict[str, int]]] = {}
_GROUPS_CACHE_TTL = 90


def _groups_cache_get(sid: str):
    entry = _GROUPS_CACHE.get(sid)
    if not entry:
        return None
    cached_at, groups = entry
    if _time.time() - cached_at > _GROUPS_CACHE_TTL:
        _GROUPS_CACHE.pop(sid, None)
        return None
    return groups


def _groups_cache_put(sid: str, groups: list) -> None:
    _GROUPS_CACHE[sid] = (_time.time(), groups)


def _owner_counts_cache_get(sid: str):
    entry = _OWNER_COUNTS_CACHE.get(sid)
    if not entry:
        return None
    cached_at, counts = entry
    if _time.time() - cached_at > _GROUPS_CACHE_TTL:
        _OWNER_COUNTS_CACHE.pop(sid, None)
        return None
    return counts


def _owner_counts_cache_put(sid: str, counts: dict[str, int]) -> None:
    _OWNER_COUNTS_CACHE[sid] = (_time.time(), counts)


def _groups_cache_clear(sid: str) -> None:
    _GROUPS_CACHE.pop(sid, None)
    _OWNER_COUNTS_CACHE.pop(sid, None)


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
    refresh = request.args.get("refresh") == "1"

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    PAGE_SIZE = 100

    sid = session.get("sid") or ""
    if refresh and sid:
        _users_cache_clear(sid)

    client = GraphClient(get_access_token())
    try:
        if search:
            # Search path: $search returns the first matching page (Graph
            # caps it). Type filter is best-effort client-side on the
            # returned slice. Search results aren't cached - the term
            # changes every keystroke and the result set is already small.
            users = list_users_sorted(client, search=search)
            if type_filter == "members":
                users = [u for u in users if u.user_type == "Member"]
            elif type_filter == "guests":
                users = [u for u in users if u.user_type == "Guest"]
            cache_key = None
        else:
            # Browse path: push the userType filter to Graph and walk every
            # @odata.nextLink so the listing reflects the whole tenant,
            # not just the first 100 names. Result is cached per (sid,
            # type_filter) for 90 seconds so switching filter chips after
            # the first hit is instant.
            cache_key = type_filter
            cached = _users_cache_get(sid, cache_key) if sid else None
            if cached is not None:
                users = cached
            else:
                wanted = {"members": "Member", "guests": "Guest"}.get(type_filter)
                users = client.list_users_by_type(wanted)
                users.sort(key=lambda u: (u.display_name or "").lower())
                if sid:
                    _users_cache_put(sid, cache_key, users)

        total = len(users)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, total_pages)
        start = (page - 1) * PAGE_SIZE
        visible = users[start : start + PAGE_SIZE]

        return render_template(
            "users_list.html",
            users=visible,
            user=current_user(),
            search=search or "",
            type_filter=type_filter,
            page=page,
            total_pages=total_pages,
            total=total,
            page_size=PAGE_SIZE,
            range_start=(start + 1) if total else 0,
            range_end=start + len(visible),
        )
    except GraphError as exc:
        flash(f"Failed to load users: {exc.message}", "error")
        return render_template(
            "users_list.html",
            users=[],
            user=current_user(),
            search=search or "",
            type_filter=type_filter,
            page=1, total_pages=1, total=0, page_size=PAGE_SIZE,
            range_start=0, range_end=0,
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


# ---------- per-user group memberships (lazy-loaded by the Users page) ----
@main_bp.route("/users/<user_id>/groups")
@login_required
def user_groups_json(user_id: str):
    """Return the groups this user is a member of, as JSON."""
    client = GraphClient(get_access_token())
    try:
        groups = client.list_user_groups(user_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({
        "groups": sorted(
            [{"id": g.id, "name": g.display_name} for g in groups],
            key=lambda g: (g["name"] or "").lower(),
        )
    })


@main_bp.route("/users/<user_id>/groups/<group_id>/remove", methods=["POST"])
@login_required
def user_group_remove(user_id: str, group_id: str):
    """Remove the user from one group. Idempotent: 'not in group' is success."""
    client = GraphClient(get_access_token())
    try:
        outcome = client.remove_member_from_group(group_id, user_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({"status": outcome})


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
    remove_missing = bool(payload.get("remove_missing"))
    delete_missing = bool(payload.get("delete_missing"))
    # Safety belt: delete is only meaningful as a sub-option of remove.
    if delete_missing and not remove_missing:
        delete_missing = False
    client = GraphClient(get_access_token())
    return _sse_response(
        iter_apply_mappings(
            client, rows,
            assignments=assignments,
            include_unknown=include_unknown,
            remove_missing=remove_missing,
            delete_missing=delete_missing,
        )
    )


# ---------- store mode preview & apply ----------
def _five_nearest_groups(query: str, all_groups, top_n: int = 5):
    """Return up to top_n group dicts whose displayName is closest to query.

    Scores against both "KFC <query>" and bare "<query>" so a store called
    "Salamander Bay" finds a group called "Salamander Bay" even when the
    KFC- prefix is missing.
    """
    import difflib

    q_lower = (query or "").strip().lower()
    if not q_lower:
        return []
    targets = (q_lower, f"kfc {q_lower}")
    scored = []
    for g in all_groups:
        name = (g.display_name or "").strip()
        if not name:
            continue
        name_lower = name.lower()
        best = max(difflib.SequenceMatcher(None, t, name_lower).ratio() for t in targets)
        scored.append((best, g))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"id": g.id, "name": g.display_name, "score": round(score, 2)}
        for score, g in scored[:top_n]
        if score > 0.3
    ]


@main_bp.route("/report/preview/<upload_id>/stores")
@login_required
def report_preview_stores(upload_id: str):
    """Render the store-mode preview of an already-uploaded report."""
    rows = _load_upload(upload_id)
    if rows is None:
        flash("Upload expired or not found - please re-upload the report.", "error")
        return redirect(url_for("main.report_upload_page"))

    stores = group_by_store(rows)
    saved = load_store_mappings()["mappings"]

    groups_json: list[dict] = []
    groups_error = None
    all_groups = []
    client = GraphClient(get_access_token())
    try:
        all_groups = client.list_groups()
        groups_json = [{"id": g.id, "name": g.display_name} for g in all_groups]
    except GraphError as exc:
        groups_error = exc.message

    # Per-store: expected "KFC <store>" target + exact match + 5-nearest fallback.
    name_to_group = {g.display_name: g for g in all_groups if g.display_name}
    store_info: dict[str, dict] = {}
    for key, sg in stores.items():
        if sg.is_unknown:
            store_info[key] = {
                "expected_name": sg.expected_group_name,
                "exact_match": None,
                "suggestions": [],
                "saved_mapping": None,
            }
            continue
        expected = sg.expected_group_name
        exact = name_to_group.get(expected)
        store_info[key] = {
            "expected_name": expected,
            "exact_match": ({"id": exact.id, "name": exact.display_name} if exact else None),
            "suggestions": [] if exact else _five_nearest_groups(sg.name, all_groups),
            "saved_mapping": saved.get(sg.name),
        }

    return render_template(
        "report_preview_stores.html",
        user=current_user(),
        upload_id=upload_id,
        stores=stores,
        store_info=store_info,
        total_rows=len(rows),
        groups_json=groups_json,
        groups_error=groups_error,
    )


@main_bp.route("/report/apply-stores/stream", methods=["POST"])
@login_required
def report_apply_stores_stream():
    """SSE stream that applies Store -> Group assignments to a parsed upload."""
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
    remove_missing = bool(payload.get("remove_missing"))
    delete_missing = bool(payload.get("delete_missing"))
    promote_community_admins = bool(payload.get("promote_community_admins"))
    if delete_missing and not remove_missing:
        delete_missing = False

    # Persist the chosen store mappings so the next upload remembers them.
    for store, mapping in assignments.items():
        gid = (mapping.get("group_id") or "").strip()
        gname = (mapping.get("group_name") or "").strip()
        if gid and gname:
            save_store_mapping(store, gid, gname)

    client = GraphClient(get_access_token())
    return _sse_response(
        iter_apply_mappings(
            client, rows,
            assignments=assignments,
            include_unknown=include_unknown,
            remove_missing=remove_missing,
            delete_missing=delete_missing,
            grouping="store",
            promote_community_admins=promote_community_admins,
        )
    )


# ============================================================================
# Groups page - browse every Entra group, see owners (community admins) and
# members, filter by Store / Franchisee / has-admins / no-admins.
# ============================================================================

def _categorise_group(group, franchisee_group_ids: set[str]) -> str:
    """Bucket a group as 'store' / 'franchisee' / 'other'.

    Store groups are identified by the same convention the apply flow uses:
    a display name beginning with "KFC " (case-insensitive). Franchisee
    groups are identified by id membership in the saved franchisee mappings.
    """
    if group.id in franchisee_group_ids:
        return "franchisee"
    name = (group.display_name or "").strip().lower()
    if name.startswith("kfc "):
        return "store"
    return "other"


@main_bp.route("/groups")
@login_required
def groups_list():
    type_filter = (request.args.get("type") or "all").lower()
    admin_filter = (request.args.get("admin") or "any").lower()
    if type_filter not in ("all", "stores", "franchisees", "other"):
        type_filter = "all"
    if admin_filter not in ("any", "has", "none"):
        admin_filter = "any"
    refresh = request.args.get("refresh") == "1"

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    PAGE_SIZE = 50

    sid = session.get("sid") or ""
    if refresh and sid:
        _groups_cache_clear(sid)

    client = GraphClient(get_access_token())
    cached_groups = _groups_cache_get(sid) if sid else None
    groups_error = None
    if cached_groups is not None:
        all_groups = cached_groups
    else:
        try:
            all_groups = client.list_groups()
            if sid:
                _groups_cache_put(sid, all_groups)
        except GraphError as exc:
            flash(f"Failed to load groups: {exc.message}", "error")
            all_groups = []
            groups_error = exc.message

    # Owner counts power the "has admins / no admins" filter. We only
    # fetch them when the user actually picks one of those chips, then
    # cache for the same TTL. The first such request can take a moment
    # on big tenants ($batch of 20 owner-count calls per HTTP, ~10
    # round-trips for 200 groups); cache hides the cost on filter swaps.
    owner_counts: dict[str, int] | None = (
        _owner_counts_cache_get(sid) if sid else None
    )
    if admin_filter != "any" and owner_counts is None:
        owner_counts = {}
        try:
            ids = [g.id for g in all_groups]
            for i in range(0, len(ids), 20):
                chunk = ids[i : i + 20]
                owner_counts.update(client.batch_count_group_owners(chunk))
            if sid:
                _owner_counts_cache_put(sid, owner_counts)
        except GraphError as exc:
            flash(
                f"Couldn't load owner counts for the admin filter: {exc.message}",
                "warning",
            )
            owner_counts = {}

    franchisee_group_ids = {
        m["group_id"] for m in load_mappings()["mappings"].values()
    }

    decorated = []
    for g in all_groups:
        cat = _categorise_group(g, franchisee_group_ids)
        decorated.append({
            "id": g.id,
            "display_name": g.display_name or "",
            "description": g.description,
            "mail_nickname": g.mail_nickname,
            "category": cat,
            "owner_count": (owner_counts or {}).get(g.id),
        })

    if type_filter == "stores":
        decorated = [g for g in decorated if g["category"] == "store"]
    elif type_filter == "franchisees":
        decorated = [g for g in decorated if g["category"] == "franchisee"]
    elif type_filter == "other":
        decorated = [g for g in decorated if g["category"] == "other"]

    if admin_filter == "has":
        decorated = [g for g in decorated if (g["owner_count"] or 0) > 0]
    elif admin_filter == "none":
        decorated = [g for g in decorated if (g["owner_count"] or 0) == 0]

    decorated.sort(key=lambda g: g["display_name"].lower())

    counts = {
        "all": len(all_groups),
        "stores": sum(
            1 for g in all_groups
            if _categorise_group(g, franchisee_group_ids) == "store"
        ),
        "franchisees": sum(
            1 for g in all_groups
            if _categorise_group(g, franchisee_group_ids) == "franchisee"
        ),
        "other": sum(
            1 for g in all_groups
            if _categorise_group(g, franchisee_group_ids) == "other"
        ),
    }

    total = len(decorated)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * PAGE_SIZE
    visible = decorated[start : start + PAGE_SIZE]

    return render_template(
        "groups_list.html",
        user=current_user(),
        groups=visible,
        type_filter=type_filter,
        admin_filter=admin_filter,
        admin_data_loaded=owner_counts is not None,
        counts=counts,
        page=page,
        total_pages=total_pages,
        total=total,
        range_start=(start + 1) if total else 0,
        range_end=start + len(visible),
        groups_error=groups_error,
    )


@main_bp.route("/groups/<group_id>/details")
@login_required
def group_details_json(group_id: str):
    """JSON: owners and members of one group (used by row expand)."""
    client = GraphClient(get_access_token())
    try:
        owners = client.list_group_owners(group_id)
        members = client.list_group_members(group_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502

    owner_ids = {o.id for o in owners}

    def _u(u) -> dict:
        return {
            "id": u.id,
            "name": u.display_name,
            "email": u.mail or u.user_principal_name,
        }

    return jsonify({
        "owners": sorted(
            [_u(o) for o in owners],
            key=lambda d: (d["name"] or "").lower(),
        ),
        "members": sorted(
            [_u(m) for m in members if m.id not in owner_ids],
            key=lambda d: (d["name"] or "").lower(),
        ),
    })


@main_bp.route("/groups/<group_id>/members/<user_id>/remove", methods=["POST"])
@login_required
def group_remove_member(group_id: str, user_id: str):
    """Remove a user from a group (Graph DELETE /members/$ref)."""
    client = GraphClient(get_access_token())
    try:
        outcome = client.remove_member_from_group(group_id, user_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({"status": outcome})


@main_bp.route("/groups/<group_id>/owners/<user_id>/remove", methods=["POST"])
@login_required
def group_remove_owner(group_id: str, user_id: str):
    """Demote a community admin (DELETE /owners/$ref). User stays in the group."""
    client = GraphClient(get_access_token())
    try:
        outcome = client.remove_owner_from_group(group_id, user_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({"status": outcome})


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


# ============================================================================
# Bulk offboard - upload CSV/XLSX of emails, optionally disable accounts
# ============================================================================

# Same in-memory + on-disk pattern as the report uploads.
_OFFBOARD_STORE: OrderedDict[str, list[str]] = OrderedDict()
_OFFBOARD_STORE_MAX = 5


def _offboard_dir():
    from .mappings import config_dir
    d = config_dir() / "offboards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stash_offboard(emails: list[str]) -> str:
    token = uuid.uuid4().hex
    _OFFBOARD_STORE[token] = list(emails)
    while len(_OFFBOARD_STORE) > _OFFBOARD_STORE_MAX:
        _OFFBOARD_STORE.popitem(last=False)
    try:
        with open(_offboard_dir() / f"{token}.json", "w", encoding="utf-8") as fh:
            json.dump(emails, fh)
    except OSError:
        pass
    return token


def _load_offboard(token: str) -> list[str] | None:
    raw = _OFFBOARD_STORE.get(token or "")
    if raw is None and token:
        try:
            with open(_offboard_dir() / f"{token}.json", encoding="utf-8") as fh:
                raw = json.load(fh)
            _OFFBOARD_STORE[token] = raw
        except (OSError, ValueError):
            raw = None
    return raw


@main_bp.route("/offboard")
@login_required
def offboard_upload_page():
    return render_template("offboard_upload.html", user=current_user())


@main_bp.route("/offboard/upload", methods=["POST"])
@login_required
def offboard_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please pick a CSV / TSV / XLSX file to upload.", "error")
        return redirect(url_for("main.offboard_upload_page"))
    content = file.read()
    try:
        emails = parse_offboard_emails(content, file.filename)
    except Exception as exc:  # parser raised
        flash(f"Couldn't read that file: {exc}", "error")
        return redirect(url_for("main.offboard_upload_page"))
    if not emails:
        flash("No email addresses found in that file.", "warning")
        return redirect(url_for("main.offboard_upload_page"))
    token = _stash_offboard(emails)
    return redirect(url_for("main.offboard_preview", upload_id=token))


@main_bp.route("/offboard/preview/<upload_id>")
@login_required
def offboard_preview(upload_id: str):
    emails = _load_offboard(upload_id)
    if emails is None:
        flash("Upload expired or not found - please re-upload.", "error")
        return redirect(url_for("main.offboard_upload_page"))
    return render_template(
        "offboard_preview.html",
        user=current_user(),
        upload_id=upload_id,
        emails=emails,
        total=len(emails),
    )


@main_bp.route("/offboard/apply/stream", methods=["POST"])
@login_required
def offboard_apply_stream():
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("upload_id") or ""
    emails = _load_offboard(upload_id)
    if emails is None:
        return _sse_response(
            [{"type": "error",
              "message": "Upload expired or not found - please re-upload."}]
        )
    disable_accounts = bool(payload.get("disable_accounts"))
    client = GraphClient(get_access_token())
    return _sse_response(
        iter_bulk_offboard(client, emails, disable_accounts=disable_accounts)
    )


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
