"""Flask app factory and routes for the Entra User Manager."""
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
from . import licensing
from .bulk import (
    iter_apply_mappings,
    iter_bulk_offboard,
    iter_convert_to_member,
    iter_cross_reference,
    iter_invite_missing,
    iter_resend_all_pending,
    iter_resend_pending_invites,
    parse_offboard_emails,
)
from .graph_client import GraphClient, GraphError
from . import cloud_mappings
from . import jobs as job_registry
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


# Per-upload filter stats so the preview can say "kept X of Y after
# dropping Z by JOBROLE filter" - mirrors _UPLOAD_STORE semantics.
_UPLOAD_STATS: OrderedDict[str, dict] = OrderedDict()


def _stash_upload(rows: list[EmployeeRow], stats=None) -> str:
    token = uuid.uuid4().hex
    payload = [r.to_dict() for r in rows]
    _UPLOAD_STORE[token] = payload
    while len(_UPLOAD_STORE) > _UPLOAD_STORE_MAX:
        _UPLOAD_STORE.popitem(last=False)
    stats_dict: dict = {}
    if stats is not None:
        # dataclasses.asdict would import dataclasses; cheap enough to
        # spell out the fields and skip the dep.
        stats_dict = {
            "total_raw": stats.total_raw,
            "kept": stats.kept,
            "dropped_status": stats.dropped_status,
            "dropped_brand": stats.dropped_brand,
            "dropped_country": stats.dropped_country,
            "dropped_job_role": stats.dropped_job_role,
            "dropped_blank": stats.dropped_blank,
            "has_status_column": stats.has_status_column,
            "has_brand_column": stats.has_brand_column,
            "has_country_column": stats.has_country_column,
            "has_job_role_column": stats.has_job_role_column,
            "total_dropped": stats.total_dropped,
        }
    _UPLOAD_STATS[token] = stats_dict
    while len(_UPLOAD_STATS) > _UPLOAD_STORE_MAX:
        _UPLOAD_STATS.popitem(last=False)
    # Persist to disk too - a 52k-row report takes ~30 MB of JSON which is
    # cheap, and surviving an app restart is worth a lot of UX.
    try:
        with open(_uploads_dir() / f"{token}.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        with open(_uploads_dir() / f"{token}.stats.json", "w", encoding="utf-8") as fh:
            json.dump(stats_dict, fh)
        _trim_uploads_dir()
    except OSError:
        pass  # disk full / permissions - in-memory copy still works
    return token


def _load_upload_stats(token: str) -> dict:
    if token in _UPLOAD_STATS:
        return _UPLOAD_STATS[token]
    try:
        with open(_uploads_dir() / f"{token}.stats.json", encoding="utf-8") as fh:
            data = json.load(fh)
        _UPLOAD_STATS[token] = data
        return data
    except (OSError, ValueError):
        return {}


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


# Per-upload email -> user_id lookup, populated by the preview's "Check
# against tenant" button. Apply routes prefer this over re-querying Graph
# so the resolve phase isn't repeated. Persisted alongside the upload
# json so closing the app between preview and apply doesn't lose it.
_LOOKUPS_STORE: OrderedDict[str, dict[str, str | None]] = OrderedDict()


def _lookup_path(token: str):
    return _uploads_dir() / f"{token}.lookup.json"


def _stash_lookup(token: str, lookup: dict[str, str | None]) -> None:
    _LOOKUPS_STORE[token] = lookup
    while len(_LOOKUPS_STORE) > _UPLOAD_STORE_MAX:
        _LOOKUPS_STORE.popitem(last=False)
    try:
        with open(_lookup_path(token), "w", encoding="utf-8") as fh:
            json.dump(lookup, fh)
    except OSError:
        pass


def _load_lookup(token: str) -> dict[str, str | None]:
    """Return the cached email lookup or an empty dict if none stashed."""
    cached = _LOOKUPS_STORE.get(token or "")
    if cached is not None:
        return cached
    if not token:
        return {}
    try:
        with open(_lookup_path(token), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _LOOKUPS_STORE[token] = data
            return data
    except (OSError, ValueError):
        pass
    return {}


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


def _job_stream_response(job, since: int = 0) -> Response:
    """Stream a Job's events as SSE with id: offsets for client reconnect.

    Each event line includes ``id: <offset>`` so the browser's reconnect
    loop can resume from the right place via ``?since=<offset>``. Idle
    timeouts yield a heartbeat comment instead of a data event so the
    client's event parser never sees them.
    """

    def generate():
        for offset, ev in job.iter_events(since=since):
            if ev is None:
                yield ": heartbeat\n\n"
                continue
            yield f"id: {offset}\ndata: {json.dumps(ev)}\n\n"
        # Final marker so the client knows this is a clean end-of-stream
        # rather than a network drop it should retry from.
        yield f"data: {json.dumps({'type': 'stream_end', 'state': job.state})}\n\n"

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


# ---------- background-job streaming + control --------------------------------
@main_bp.route("/jobs/<job_id>/stream")
@login_required
def job_stream(job_id: str):
    """SSE stream of a job's events from ?since=<offset>. Client reconnects
    here with the offset of the last event they saw - any events appended
    while they were disconnected get replayed immediately."""
    try:
        since = max(0, int(request.args.get("since") or 0))
    except (TypeError, ValueError):
        since = 0
    job = job_registry.get(job_id)
    if not job:
        return _sse_response(
            [{"type": "error",
              "message": "Job not found (it may have finished and been pruned)."}]
        )
    return _job_stream_response(job, since=since)


@main_bp.route("/jobs/active")
@login_required
def jobs_active():
    """List running jobs - used by the floating actions dock on every page."""
    return jsonify(jobs=job_registry.list_active())


@main_bp.route("/jobs/<job_id>", methods=["GET"])
@login_required
def job_snapshot(job_id: str):
    job = job_registry.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(job=job.snapshot())


@main_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def job_cancel(job_id: str):
    job = job_registry.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    job.request_cancel()
    return jsonify(ok=True)


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
    if type_filter not in ("all", "members", "guests", "pending"):
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
            elif type_filter == "pending":
                users = [u for u in users if u.external_user_state == "PendingAcceptance"]
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
                if type_filter == "pending":
                    # Dedicated Graph filter - same query the Re-invites
                    # scan uses, so the two views always agree.
                    users = client.list_pending_acceptance_users()
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
    except Exception as exc:  # noqa: BLE001 - keep response JSON-shaped
        return jsonify({"error": str(exc) or "Unexpected server error."}), 500
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
        rows, stats = parse_report(data, file.filename)
    except ReportParseError as exc:
        flash(f"Could not parse '{file.filename}': {exc}", "error")
        return redirect(url_for("main.report_upload_page"))

    upload_id = _stash_upload(rows, stats=stats)
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
        filter_stats=_load_upload_stats(upload_id),
        saved_mappings=saved,
        groups_json=groups_json,
        groups_error=groups_error,
    )


@main_bp.route("/report/apply/stream", methods=["POST"])
@login_required
def report_apply_stream():
    """Kick off a Franchisee-mode apply job and return its job_id.

    The work runs on a background thread (kfc_entra.jobs) so the client
    can disconnect, reconnect, and pick up where it left off. The
    response is JSON; the client then opens an SSE stream at
    /jobs/<id>/stream?since=0 to follow progress.
    """
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("upload_id") or ""
    rows = _load_upload(upload_id)
    if rows is None:
        return jsonify(
            error="Upload expired or not found - please re-upload the report.",
        ), 404
    assignments = payload.get("assignments") or {}
    if not isinstance(assignments, dict):
        assignments = {}
    include_unknown = bool(payload.get("include_unknown"))
    remove_missing = bool(payload.get("remove_missing"))
    delete_missing = bool(payload.get("delete_missing"))
    invite_missing = bool(payload.get("invite_missing"))
    send_invitation_message = bool(payload.get("send_invitation_message", True))
    retry_emails = payload.get("retry_emails") or None
    if retry_emails is not None and not isinstance(retry_emails, list):
        retry_emails = None
    # Safety belt: delete is only meaningful as a sub-option of remove.
    if delete_missing and not remove_missing:
        delete_missing = False
    # On retry, never reconcile / delete - we're only re-trying the
    # exact rows that failed last time, not redoing the whole pass.
    if retry_emails:
        remove_missing = False
        delete_missing = False
    client = GraphClient(get_access_token())
    cached_lookup = _load_lookup(upload_id) or None

    if retry_emails:
        label = f"Retry failed rows ({len(retry_emails)})"
    else:
        label = f"Apply by Franchisee ({len(assignments)} group(s))"
    started_from = request.referrer or url_for(
        "main.report_preview", upload_id=upload_id
    )
    cfg = current_app.config["KFC_CONFIG"]
    invite_redirect_url = cfg.invite_redirect_url

    def factory(job):
        def gen():
            for ev in iter_apply_mappings(
                client, rows,
                assignments=assignments,
                include_unknown=include_unknown,
                remove_missing=remove_missing,
                delete_missing=delete_missing,
                email_lookup=cached_lookup,
                invite_missing=invite_missing,
                invite_redirect_url=invite_redirect_url,
                send_invitation_message=send_invitation_message,
                email_allowlist=set(retry_emails) if retry_emails else None,
            ):
                if job.cancel_requested:
                    return
                yield ev
            _cloud_push_silent(client)
        return gen()

    job = job_registry.start("apply_franchisee", label, started_from, factory)
    return jsonify(job_id=job.id, stream_url=url_for(
        "main.job_stream", job_id=job.id
    ))


@main_bp.route("/report/preview/<upload_id>/lookup")
@login_required
def report_lookup_json(upload_id: str):
    """Return the cached email->user_id map so the preview can update
    per-row badges after the cross-reference SSE completes."""
    lookup = _load_lookup(upload_id)
    # Normalise to email-lowercase boolean for the client: emails are
    # case-insensitive in Entra, and the row's data attribute is lowercased
    # too, so the client matches without re-normalising.
    return jsonify({
        "lookup": {
            (e or "").lower(): bool(uid)
            for e, uid in lookup.items()
        }
    })


@main_bp.route("/report/preview/<upload_id>/cross-reference", methods=["POST"])
@login_required
def report_cross_reference(upload_id: str):
    """SSE: cross-reference every report email against the tenant.

    The Yammer ID column in the report only flips to a UUID once a user
    activates Viva Engage, so trusting it for "is in Entra" would mark
    pre-invited guests as missing. This route walks the unique emails
    instead, batched 20 at a time via Graph $batch, and stashes the
    {email: user_id|None} map alongside the upload so the apply step
    can re-use it without another lookup pass.
    """
    rows = _load_upload(upload_id)
    if rows is None:
        return _sse_response(
            [{"type": "error",
              "message": "Upload expired or not found - please re-upload the report."}]
        )
    emails = [(r.email or "").strip() for r in rows if (r.email or "").strip()]
    client = GraphClient(get_access_token())

    # Reuse anything we've already looked up so re-running the check is cheap.
    lookup = dict(_load_lookup(upload_id))
    fresh_emails = [e for e in emails if e not in lookup]

    def stream():
        yield from iter_cross_reference(client, fresh_emails, lookup)
        _stash_lookup(upload_id, lookup)

    return _sse_response(stream())


@main_bp.route("/report/preview/<upload_id>/invite-missing/stream", methods=["POST"])
@login_required
def report_invite_missing(upload_id: str):
    """Kick off a job that invites every report row whose email didn't
    resolve during cross-reference. Returns ``{job_id, stream_url}`` so
    the bulk panel + dock light up like every other apply.

    The user must have run cross-reference first - we only invite the
    cached "not in tenant" emails to avoid surprise invitations.
    """
    rows = _load_upload(upload_id)
    if rows is None:
        return jsonify(
            error="Upload expired or not found - please re-upload the report.",
        ), 404

    lookup = _load_lookup(upload_id) or {}
    payload = request.get_json(silent=True) or {}
    send_invitation_message = bool(payload.get("send_invitation_message", True))

    # Build the candidate list: email + best-effort display_name from the
    # first row that mentions each email. Skip dupes and rows whose email
    # is already known to be in tenant.
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        e = (r.email or "").strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        # Only invite emails we've checked and confirmed are NOT in tenant.
        if lookup.get(e):
            continue
        if e not in lookup:
            continue  # never checked yet - require xref first
        candidates.append((e, r.name or ""))

    if not candidates:
        return jsonify(
            error=(
                "Nothing to invite. Run \"Check against tenant\" first, "
                "or all checked emails already exist in the tenant."
            ),
        ), 400

    client = GraphClient(get_access_token())
    cfg = current_app.config["KFC_CONFIG"]
    invite_redirect_url = cfg.invite_redirect_url

    label = f"Invite missing users ({len(candidates)})"
    started_from = request.referrer or url_for(
        "main.report_preview", upload_id=upload_id
    )

    def factory(job):
        def gen():
            for ev in iter_invite_missing(
                client,
                candidates,
                invite_redirect_url=invite_redirect_url,
                send_invitation_message=send_invitation_message,
            ):
                if job.cancel_requested:
                    return
                # Update the xref cache as invites land so a follow-up
                # cross-reference / apply doesn't try to invite them
                # again.
                if ev.get("type") == "progress" and ev.get("status") == "invited":
                    # The iterator doesn't pass uid through the event,
                    # but for the cache we only need "is now in tenant"
                    # which is true the moment Graph accepted the
                    # invite. Mark with a sentinel that callers treat
                    # as truthy until the next real cross-reference.
                    lookup[ev.get("user")] = "invited-pending"
                yield ev
            _stash_lookup(upload_id, lookup)
        return gen()

    job = job_registry.start("invite_missing", label, started_from, factory)
    return jsonify(job_id=job.id, stream_url=url_for(
        "main.job_stream", job_id=job.id
    ))


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
        filter_stats=_load_upload_stats(upload_id),
        groups_json=groups_json,
        groups_error=groups_error,
    )


@main_bp.route("/report/apply-stores/stream", methods=["POST"])
@login_required
def report_apply_stores_stream():
    """Kick off a Store-mode apply job. Same job-manager pattern as
    /report/apply/stream - returns JSON {job_id, stream_url} for the
    client to subscribe to."""
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("upload_id") or ""
    rows = _load_upload(upload_id)
    if rows is None:
        return jsonify(
            error="Upload expired or not found - please re-upload the report.",
        ), 404
    assignments = payload.get("assignments") or {}
    if not isinstance(assignments, dict):
        assignments = {}
    include_unknown = bool(payload.get("include_unknown"))
    remove_missing = bool(payload.get("remove_missing"))
    delete_missing = bool(payload.get("delete_missing"))
    promote_community_admins = bool(payload.get("promote_community_admins"))
    invite_missing = bool(payload.get("invite_missing"))
    send_invitation_message = bool(payload.get("send_invitation_message", True))
    retry_emails = payload.get("retry_emails") or None
    if retry_emails is not None and not isinstance(retry_emails, list):
        retry_emails = None
    if delete_missing and not remove_missing:
        delete_missing = False
    if retry_emails:
        remove_missing = False
        delete_missing = False

    # Persist the chosen store mappings so the next upload remembers them.
    # Skip on retry - the mappings are already saved from the original run.
    saved_any = False
    if not retry_emails:
        for store, mapping in assignments.items():
            gid = (mapping.get("group_id") or "").strip()
            gname = (mapping.get("group_name") or "").strip()
            if gid and gname:
                save_store_mapping(store, gid, gname)
                saved_any = True

    client = GraphClient(get_access_token())
    if saved_any:
        _cloud_push_silent(client)
    cached_lookup = _load_lookup(upload_id) or None

    if retry_emails:
        label = f"Retry failed rows by Store ({len(retry_emails)})"
    else:
        label = f"Apply by Store ({len(assignments)} group(s))"
    started_from = request.referrer or url_for(
        "main.report_preview_stores", upload_id=upload_id
    )
    cfg = current_app.config["KFC_CONFIG"]
    invite_redirect_url = cfg.invite_redirect_url

    def factory(job):
        def gen():
            for ev in iter_apply_mappings(
                client, rows,
                assignments=assignments,
                include_unknown=include_unknown,
                remove_missing=remove_missing,
                delete_missing=delete_missing,
                grouping="store",
                promote_community_admins=promote_community_admins,
                email_lookup=cached_lookup,
                invite_missing=invite_missing,
                invite_redirect_url=invite_redirect_url,
                send_invitation_message=send_invitation_message,
                email_allowlist=set(retry_emails) if retry_emails else None,
            ):
                if job.cancel_requested:
                    return
                yield ev
        return gen()

    job = job_registry.start("apply_stores", label, started_from, factory)
    return jsonify(job_id=job.id, stream_url=url_for(
        "main.job_stream", job_id=job.id
    ))


# ============================================================================
# Groups page - browse every Entra group, see owners (community admins) and
# members, filter by Store / Franchisee / has-admins / no-admins.
# ============================================================================

def _categorise_group(group, franchisee_group_ids: set[str]) -> str:
    """Bucket a group as 'store' / 'franchisee' / 'other'.

    The display name is the authoritative signal: anything starting with
    "KFC " (case-insensitive) is a Store group, period. Only after that
    do we consult the saved franchisee mappings, so a Store id that's
    been accidentally written into mappings.json (legacy data) still
    shows up correctly as a Store - not a Franchisee.
    """
    name = (group.display_name or "").strip().lower()
    if name.startswith("kfc "):
        return "store"
    if group.id in franchisee_group_ids:
        return "franchisee"
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


@main_bp.route("/groups/<group_id>/members/<user_id>/promote", methods=["POST"])
@login_required
def group_promote_member(group_id: str, user_id: str):
    """Promote an existing group member to owner (community admin).

    Calls Graph POST /groups/{id}/owners/$ref. The user stays in the
    members list too - Graph treats owners and members as separate
    relationships. If the user is already an owner, the Graph 4xx is
    mapped to status="already_owner" so the UI can no-op cleanly.
    """
    client = GraphClient(get_access_token())
    try:
        outcome = client.add_owner_to_group(group_id, user_id)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    return jsonify({"status": outcome})


# ---------- mappings management ----------
def _cloud_push_silent(client: GraphClient | None = None) -> None:
    """Best-effort: push local mappings to the cloud after a save/delete.

    Never raises - failures land in cloud_settings.last_error so the
    Settings page can surface them next time.
    """
    if not cloud_mappings.is_enabled():
        return
    try:
        if client is None:
            client = GraphClient(get_access_token())
        cloud_mappings.push_all(client)
    except Exception:  # noqa: BLE001 - best-effort sync, never blocks save
        pass


@main_bp.route("/report/mappings")
@login_required
def report_mappings():
    sync_warning = None
    if cloud_mappings.is_enabled():
        try:
            client = GraphClient(get_access_token())
            _settings, fz_added, store_added = cloud_mappings.pull_all(client)
            if fz_added or store_added:
                bits = []
                if fz_added:
                    bits.append(f"{fz_added} Franchisee")
                if store_added:
                    bits.append(f"{store_added} Store")
                flash(
                    "Pulled " + " and ".join(bits) + " mapping(s) from cloud.",
                    "success",
                )
        except Exception as exc:  # noqa: BLE001
            sync_warning = str(exc)

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
        cloud_settings=cloud_mappings.load_settings(),
        sync_warning=sync_warning,
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
    client: GraphClient | None = None
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
    _cloud_push_silent(client)
    return redirect(url_for("main.report_mappings"))


@main_bp.route("/report/mappings/<code>/delete", methods=["POST"])
@login_required
def report_mappings_delete(code: str):
    if delete_mapping(code):
        flash(f"Mapping for {code.upper()} deleted.", "success")
        _cloud_push_silent()
    else:
        flash(f"No mapping found for {code.upper()}.", "warning")
    return redirect(url_for("main.report_mappings"))


# ---------- settings (cloud-sync) -------------------------------------------
@main_bp.route("/settings")
@login_required
def settings_page():
    settings = cloud_mappings.load_settings()
    cfg = current_app.config["KFC_CONFIG"]
    me = None
    if settings.get("enabled"):
        try:
            client = GraphClient(get_access_token())
            me = client.me()
        except Exception as exc:  # noqa: BLE001
            me = {"error": str(exc)}
    return render_template(
        "settings.html",
        user=current_user(),
        cloud_settings=settings,
        active_client_id=cfg.client_id,
        active_tenant_id=cfg.tenant_id,
        me=me,
        local_dir=str(cloud_mappings.config_dir()),
    )


@main_bp.route("/settings/save", methods=["POST"])
@login_required
def settings_save():
    enabled = request.form.get("enabled") == "on"
    destination = (request.form.get("destination") or "onedrive").strip().lower()
    site_url = (request.form.get("sharepoint_site_url") or "").strip()
    fields: dict = {"enabled": enabled, "destination": destination}

    if destination == "sharepoint":
        if not site_url:
            flash("Pick OneDrive, or enter a SharePoint site URL.", "error")
            return redirect(url_for("main.settings_page"))
        try:
            client = GraphClient(get_access_token())
            site = client.resolve_sharepoint_site(site_url)
        except GraphError as exc:
            flash(f"Could not resolve SharePoint site: {exc.message}", "error")
            return redirect(url_for("main.settings_page"))
        fields["sharepoint_site_url"] = site_url
        fields["sharepoint_site_id"] = site.get("id") or ""
        fields["sharepoint_site_name"] = site.get("displayName") or site.get("name") or ""
    else:
        fields["sharepoint_site_url"] = ""
        fields["sharepoint_site_id"] = ""
        fields["sharepoint_site_name"] = ""

    cloud_mappings.save_settings(fields)

    # If sync was just turned on, do an initial push so the cloud has the
    # current local mappings to merge against on the next device.
    if enabled:
        try:
            client = GraphClient(get_access_token())
            cloud_mappings.push_all(client)
            flash("Cloud sync turned on. Local mappings pushed.", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"Saved settings, but initial push failed: {exc}", "warning")
    else:
        flash("Cloud sync turned off. Local mappings are unchanged.", "success")

    return redirect(url_for("main.settings_page"))


@main_bp.route("/settings/sync-now", methods=["POST"])
@login_required
def settings_sync_now():
    """Manual two-way sync: pull, merge, push."""
    if not cloud_mappings.is_enabled():
        flash("Cloud sync is off. Turn it on first.", "warning")
        return redirect(url_for("main.settings_page"))
    try:
        client = GraphClient(get_access_token())
        _settings, fz_added, store_added = cloud_mappings.pull_all(client)
        cloud_mappings.push_all(client)
    except Exception as exc:  # noqa: BLE001
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("main.settings_page"))
    bits = []
    if fz_added:
        bits.append(f"{fz_added} Franchisee")
    if store_added:
        bits.append(f"{store_added} Store")
    if bits:
        flash("Pulled " + " and ".join(bits) + " mapping(s) from cloud, then pushed local back.", "success")
    else:
        flash("Synced. No remote changes to merge in; local pushed.", "success")
    return redirect(url_for("main.settings_page"))


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


# Hard-coded "never touch" exemptions for the tenant-wide offboard
# scan. Anything matching one of these by directory-role display name
# (case-insensitive substring) or by email address (UPN or .mail) gets
# left alone. Keep this list short and conservative - it's the only
# brake between a single button-click and removing every member of the
# tenant from every group.
TENANT_OFFBOARD_EXEMPT_ROLE_NAMES = (
    "global administrator",
    "yammer administrator",
    "yammer",
)
TENANT_OFFBOARD_EXEMPT_EMAILS = (
    "thomasfisher2119@gmail.com",
    "yammertime@yum.com",
)


def _normalise_email(value: str) -> str:
    return (value or "").strip().lower()


def _collect_tenant_offboard_targets(client: GraphClient) -> tuple[list[str], dict]:
    """List every user in the tenant minus the hard-coded exemptions.

    Exemptions are: members of any directory role whose display name
    contains "Global Administrator" or "Yammer" (case-insensitive), plus
    every address in TENANT_OFFBOARD_EXEMPT_EMAILS matched against UPN
    OR .mail OR otherMails when present.

    Returns (emails, stats) where ``stats`` carries the counts the UI
    surfaces in the preview ("found N users, exempted K admins...").
    """
    exempt_user_ids: set[str] = set()
    exempt_emails: set[str] = {_normalise_email(e) for e in TENANT_OFFBOARD_EXEMPT_EMAILS}
    exempt_role_hits: list[dict] = []

    for role_id, role_name in client.list_directory_roles():
        lower = role_name.lower()
        if not any(needle in lower for needle in TENANT_OFFBOARD_EXEMPT_ROLE_NAMES):
            continue
        try:
            members = client.list_directory_role_members(role_id)
        except GraphError:
            members = []
        for m in members:
            exempt_user_ids.add(m.id)
        exempt_role_hits.append({"role": role_name, "member_count": len(members)})

    all_users = client.list_users_by_type()
    emails: list[str] = []
    seen_emails: set[str] = set()
    skipped_no_email = 0
    exempted_by_role = 0
    exempted_by_email = 0
    for u in all_users:
        if u.id and u.id in exempt_user_ids:
            exempted_by_role += 1
            continue
        candidate_addresses = {
            _normalise_email(u.mail or ""),
            _normalise_email(u.user_principal_name or ""),
        }
        candidate_addresses.discard("")
        if candidate_addresses & exempt_emails:
            exempted_by_email += 1
            continue
        # Prefer .mail (the real address), fall back to UPN. Skip users
        # with neither - the offboard apply matches by email so a record
        # we can't match wouldn't do anything anyway.
        addr = _normalise_email(u.mail or "") or _normalise_email(u.user_principal_name or "")
        if not addr:
            skipped_no_email += 1
            continue
        if addr in seen_emails:
            continue
        seen_emails.add(addr)
        emails.append(addr)

    stats = {
        "total_users": len(all_users),
        "target_count": len(emails),
        "exempted_by_role": exempted_by_role,
        "exempted_by_email": exempted_by_email,
        "skipped_no_email": skipped_no_email,
        "matched_roles": exempt_role_hits,
    }
    return emails, stats


@main_bp.route("/offboard/scan-tenant", methods=["POST"])
@login_required
def offboard_scan_tenant():
    """Build an offboard target list from every user in the tenant minus
    the hard-coded exemptions, stash it, and tell the UI which preview
    page to redirect to.
    """
    client = GraphClient(get_access_token())
    try:
        emails, stats = _collect_tenant_offboard_targets(client)
    except GraphError as exc:
        return jsonify({"error": exc.message}), 502
    except Exception as exc:  # noqa: BLE001 - JSON-shaped failure
        return jsonify({"error": str(exc) or "Unexpected server error."}), 500
    if not emails:
        return jsonify({
            "error": "No offboard targets found after exemptions.",
            "stats": stats,
        }), 400
    token = _stash_offboard(emails)
    return jsonify({
        "upload_id": token,
        "preview_url": url_for("main.offboard_preview", upload_id=token),
        "stats": stats,
    })


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


# ============================================================================
# Re-invites - upload CSV of emails the user believes were already invited;
# look each one up, find those still in PendingAcceptance, re-send Microsoft's
# invitation email. Mirrors the PowerShell "ResendInvitations.ps1" workflow.
# ============================================================================

# Same in-memory + on-disk stash pattern as offboards.
_REINVITE_STORE: OrderedDict[str, list[str]] = OrderedDict()
_REINVITE_STORE_MAX = 5


def _reinvite_dir():
    from .mappings import config_dir
    d = config_dir() / "reinvites"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stash_reinvite(emails: list[str]) -> str:
    token = uuid.uuid4().hex
    _REINVITE_STORE[token] = list(emails)
    while len(_REINVITE_STORE) > _REINVITE_STORE_MAX:
        _REINVITE_STORE.popitem(last=False)
    try:
        with open(_reinvite_dir() / f"{token}.json", "w", encoding="utf-8") as fh:
            json.dump(emails, fh)
    except OSError:
        pass
    return token


def _load_reinvite(token: str) -> list[str] | None:
    raw = _REINVITE_STORE.get(token or "")
    if raw is None and token:
        try:
            with open(_reinvite_dir() / f"{token}.json", encoding="utf-8") as fh:
                raw = json.load(fh)
            _REINVITE_STORE[token] = raw
        except (OSError, ValueError):
            raw = None
    return raw


@main_bp.route("/reinvites")
@login_required
def reinvites_upload_page():
    return render_template("reinvites_upload.html", user=current_user())


@main_bp.route("/reinvites/upload", methods=["POST"])
@login_required
def reinvites_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Pick a CSV / TSV / XLSX file containing email addresses.", "error")
        return redirect(url_for("main.reinvites_upload_page"))
    content = file.read()
    try:
        emails = parse_offboard_emails(content, file.filename)
    except Exception as exc:  # noqa: BLE001 - parser raised
        flash(f"Couldn't read that file: {exc}", "error")
        return redirect(url_for("main.reinvites_upload_page"))
    if not emails:
        flash("No email addresses found in that file.", "warning")
        return redirect(url_for("main.reinvites_upload_page"))
    token = _stash_reinvite(emails)
    return redirect(url_for("main.reinvites_preview", upload_id=token))


@main_bp.route("/reinvites/preview/<upload_id>")
@login_required
def reinvites_preview(upload_id: str):
    emails = _load_reinvite(upload_id)
    if emails is None:
        flash("Upload expired or not found - please re-upload.", "error")
        return redirect(url_for("main.reinvites_upload_page"))
    return render_template(
        "reinvites_preview.html",
        user=current_user(),
        upload_id=upload_id,
        emails=emails,
        total=len(emails),
    )


@main_bp.route("/reinvites/apply/stream", methods=["POST"])
@login_required
def reinvites_apply_stream():
    """Kick off a Job that checks each email's invitation state and
    re-sends Microsoft's invitation to the PendingAcceptance ones.

    Returns JSON {job_id, stream_url} so the bulk-panel + dock light up
    like every other managed apply.
    """
    payload = request.get_json(silent=True) or {}
    upload_id = payload.get("upload_id") or ""
    emails = _load_reinvite(upload_id)
    if emails is None:
        return jsonify(error="Upload expired or not found - please re-upload."), 404

    client = GraphClient(get_access_token())
    cfg = current_app.config["KFC_CONFIG"]
    invite_redirect_url = cfg.invite_redirect_url

    label = f"Re-send pending invitations ({len(emails)})"
    started_from = request.referrer or url_for(
        "main.reinvites_preview", upload_id=upload_id
    )

    def factory(job):
        def gen():
            for ev in iter_resend_pending_invites(
                client, emails,
                invite_redirect_url=invite_redirect_url,
            ):
                if job.cancel_requested:
                    return
                yield ev
        return gen()

    job = job_registry.start("reinvites", label, started_from, factory)
    return jsonify(job_id=job.id, stream_url=url_for(
        "main.job_stream", job_id=job.id
    ))


@main_bp.route("/reinvites/scan/stream", methods=["POST"])
@login_required
def reinvites_scan_stream():
    """Tenant-wide scan: find every PendingAcceptance Guest and resend
    their invitation. No CSV upload required - the Graph filter does
    the work."""
    client = GraphClient(get_access_token())
    cfg = current_app.config["KFC_CONFIG"]
    invite_redirect_url = cfg.invite_redirect_url

    label = "Re-send all pending tenant invitations"
    started_from = request.referrer or url_for("main.reinvites_upload_page")

    def factory(job):
        def gen():
            for ev in iter_resend_all_pending(
                client, invite_redirect_url=invite_redirect_url,
            ):
                if job.cancel_requested:
                    return
                yield ev
        return gen()

    job = job_registry.start("reinvites_scan", label, started_from, factory)
    return jsonify(job_id=job.id, stream_url=url_for(
        "main.job_stream", job_id=job.id
    ))


def _display_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    parts = [p for p in local.replace("_", ".").split(".") if p]
    return " ".join(p.capitalize() for p in parts) if parts else email


# ============================================================================
# Paywall - 14-day free trial, then a server-validated license key. Anything
# but a "trial" or "licensed" state blocks the app at sign-in (Settings,
# /license and /license/* are the only routes that stay reachable so the user
# can still paste a key or read the buy link).
# ============================================================================

# Endpoints that stay reachable when the app is locked.
_LICENSE_OPEN_ENDPOINTS = frozenset(
    {
        "main.license_page",
        "main.license_save",
        "main.license_check",
        "main.license_clear",
        "main.install_update",
        "static",
    }
)


_LAUNCH_RECHECK_DONE = False


def _current_license_state():
    """Read the current license state, forcing a server round-trip the
    first time it's called after the app launches.

    The cache inside licensing.py is sized for 24h so we don't beat up
    the worker on every page load, but that means a revocation issued
    while the user is away wouldn't take effect until tomorrow without
    this nudge. Running once per process restart catches it on the next
    app open while still keeping the rest of the session cache-fast.
    """
    global _LAUNCH_RECHECK_DONE
    cfg = current_app.config["KFC_CONFIG"]
    force = not _LAUNCH_RECHECK_DONE
    state = licensing.current_entitlement(
        server_url=cfg.license_server_url,
        tenant_id=cfg.tenant_id,
        app_version=__version__,
        buy_url=cfg.license_buy_url,
        force_recheck=force,
    )
    if force:
        _LAUNCH_RECHECK_DONE = True
    return state


@main_bp.route("/license", methods=["GET"])
def license_page():
    state = _current_license_state()
    return render_template(
        "license_required.html",
        user=current_user(),
        license_state=state,
        license_server_set=bool(current_app.config["KFC_CONFIG"].license_server_url),
    )


@main_bp.route("/license", methods=["POST"])
def license_save():
    key = (request.form.get("key") or "").strip()
    if not key:
        flash("Paste your license key first.", "error")
        return redirect(url_for("main.license_page"))
    licensing.save_state({"key": key, "last_check_at": "", "expires_at": ""})
    # Force a fresh server check so the user sees the verdict immediately.
    cfg = current_app.config["KFC_CONFIG"]
    state = licensing.current_entitlement(
        server_url=cfg.license_server_url,
        tenant_id=cfg.tenant_id,
        app_version=__version__,
        buy_url=cfg.license_buy_url,
        force_recheck=True,
    )
    if state.can_use:
        flash(state.message or "License accepted.", "success")
        return redirect(url_for("main.landing"))
    flash(state.message or "License rejected.", "error")
    return redirect(url_for("main.license_page"))


@main_bp.route("/license/check", methods=["POST"])
def license_check():
    """Manual 're-check now' button on the license page / Settings."""
    cfg = current_app.config["KFC_CONFIG"]
    state = licensing.current_entitlement(
        server_url=cfg.license_server_url,
        tenant_id=cfg.tenant_id,
        app_version=__version__,
        buy_url=cfg.license_buy_url,
        force_recheck=True,
    )
    flash(state.message or state.state, "success" if state.can_use else "warning")
    return redirect(url_for("main.license_page"))


@main_bp.route("/license/clear", methods=["POST"])
def license_clear():
    licensing.clear_key()
    flash("License key cleared.", "success")
    return redirect(url_for("main.license_page"))


def create_app() -> Flask:
    cfg = load_config()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["KFC_CONFIG"] = cfg
    app.secret_key = cfg.flask_secret_key

    # The failures CSV download POSTs the rows back through /downloads/csv
    # as a form field. A 50k-row report's failure list can easily be
    # several MB, so raise both the total request cap (MAX_CONTENT_LENGTH)
    # and the per-field cap (MAX_FORM_MEMORY_SIZE) - Werkzeug's defaults
    # would 413 with "Request Entity Too Large" on anything sizeable.
    # 200MB is way more than any realistic report.
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    app.config["MAX_FORM_MEMORY_SIZE"] = 200 * 1024 * 1024

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.before_request
    def _enforce_license():
        # The Flask static handler + a small allow-list of license-self-
        # service endpoints stay reachable so a locked install can still
        # paste a key. Everything else gets redirected (or JSON-401'd for
        # AJAX) to the license page.
        from .auth import _wants_json
        ep = request.endpoint or ""
        if ep in _LICENSE_OPEN_ENDPOINTS:
            return None
        state = _current_license_state()
        if state.can_use:
            return None
        if _wants_json():
            return jsonify(
                error=state.message or "License required.",
                license_state=state.state,
                buy_url=state.buy_url,
            ), 402
        return redirect(url_for("main.license_page"))

    @app.context_processor
    def inject_user():
        # Pull license state into every template so the base layout can
        # show a "Trial - 3 days remaining" banner.
        state = None
        try:
            state = _current_license_state()
        except Exception:  # noqa: BLE001 - never block render on license probe
            pass
        return {
            "current_user": current_user(),
            "app_version": __version__,
            "available_update": get_available_update(),
            "self_install_supported": self_install_supported(),
            "license_state": state,
        }

    return app
