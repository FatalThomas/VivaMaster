"""MSAL-based authentication using OAuth 2.0 device code flow.

No redirect URI, no browser callback, no chance of redirect loops. The
signed-in admin sees a short code, types it into microsoft.com/devicelogin
(in their default browser, which we offer to open for them), and we
background-poll MSAL for the resulting tokens.

The Flask session cookie is kept deliberately tiny - it holds a session
UUID (`sid`) and, while a sign-in is in progress, a device-flow UUID. The
MSAL token cache and the user identity claims live in an in-process dict
keyed by `sid`. That dodges Flask's signed-cookie size limit and the
WebView2 quirk where a Set-Cookie from a fetch response sometimes isn't
visible to the next top-level navigation.
"""
from __future__ import annotations

import threading
import time
import webbrowser
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

import msal
from flask import current_app, redirect, request, session, url_for

# Microsoft hosts we'll open in the system browser on behalf of the user.
_BROWSER_OPEN_ALLOWLIST = frozenset(
    {
        "microsoft.com",
        "www.microsoft.com",
        "login.microsoftonline.com",
        "login.live.com",
    }
)

# Pending device flows: {flow_id: {"flow", "started", "result"}}.
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL = 15 * 60


@dataclass
class _SessionState:
    cache: msal.SerializableTokenCache = field(default_factory=msal.SerializableTokenCache)
    user: dict | None = None


# Per-Flask-session state, keyed by the `sid` we put in the session cookie.
_SESSIONS: dict[str, _SessionState] = {}
_SESSIONS_LOCK = threading.Lock()


# ---------- internal helpers ----------

def _ensure_sid() -> str:
    """Return the session's stable id, creating one on first access."""
    sid = session.get("sid")
    if not sid:
        sid = uuid4().hex
        session["sid"] = sid
        session.permanent = True
    return sid


def _state_for(sid: str) -> _SessionState:
    with _SESSIONS_LOCK:
        state = _SESSIONS.get(sid)
        if state is None:
            state = _SessionState()
            _SESSIONS[sid] = state
        return state


def _purge_expired_unlocked() -> None:
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["started"] > _PENDING_TTL]:
        _PENDING.pop(k, None)


def _flow_info(flow_id: str, flow: dict) -> dict:
    verification_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
    user_code = flow["user_code"]
    return {
        "flow_id": flow_id,
        "user_code": user_code,
        "verification_uri": verification_uri,
        # Microsoft accepts ?otc=CODE on the devicelogin page to pre-fill
        # the code, so the user just signs in - no copy/paste required.
        "verification_uri_complete": f"{verification_uri}?otc={user_code}",
        "message": flow.get("message", ""),
        "expires_in": flow.get("expires_in", 900),
    }


def _build_msal_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    cfg = current_app.config["KFC_CONFIG"]
    return msal.PublicClientApplication(cfg.client_id, authority=cfg.authority, token_cache=cache)


# ---------- public API ----------

def start_device_flow() -> dict:
    """Initiate device flow and spawn the background MSAL poller.

    Idempotent: if the session already has a pending device flow with
    time left on the clock, that flow's code is reused instead of
    starting a new one. The worker uses the *same* per-session token
    cache that get_access_token() reads later, so a successful sign-in
    is immediately visible to subsequent requests.
    """
    cfg = current_app.config["KFC_CONFIG"]

    existing_id = session.get("device_flow_id")
    if existing_id:
        with _PENDING_LOCK:
            entry = _PENDING.get(existing_id)
            if entry is not None and entry["result"] is None:
                age = time.time() - entry["started"]
                # Keep a 60s buffer so we never hand back a code about to expire.
                if age < _PENDING_TTL - 60:
                    return _flow_info(existing_id, entry["flow"])

    sid = _ensure_sid()
    shared_cache = _state_for(sid).cache

    msal_app = _build_msal_app(shared_cache)
    flow = msal_app.initiate_device_flow(scopes=cfg.scopes)
    if "user_code" not in flow:
        raise RuntimeError(
            flow.get("error_description") or flow.get("error") or "Could not start device flow."
        )

    flow_id = uuid4().hex
    client_id, authority = cfg.client_id, cfg.authority

    def worker(flow_copy: dict) -> None:
        # Block on the same per-session cache so refresh tokens land
        # where get_access_token() will read them - no serialization
        # round-trip through the cookie.
        local = msal.PublicClientApplication(client_id, authority=authority, token_cache=shared_cache)
        try:
            result = local.acquire_token_by_device_flow(flow_copy)
        except Exception as exc:  # network glitch, etc.
            result = {"error": "worker_failed", "error_description": str(exc)}
        with _PENDING_LOCK:
            entry = _PENDING.get(flow_id)
            if entry is not None:
                entry["result"] = result

    with _PENDING_LOCK:
        _purge_expired_unlocked()
        _PENDING[flow_id] = {"flow": flow, "started": time.time(), "result": None}
    threading.Thread(
        target=worker,
        args=(dict(flow),),
        daemon=True,
        name=f"device-flow-{flow_id[:8]}",
    ).start()

    session["device_flow_id"] = flow_id
    return _flow_info(flow_id, flow)


def poll_device_flow() -> dict:
    """Return the current state of the pending device flow.

    {"status": "pending"} | {"status": "success"} | {"status": "expired"}
    | {"status": "error", "error": "..."}
    """
    flow_id = session.get("device_flow_id")
    if not flow_id:
        return {"status": "expired"}
    with _PENDING_LOCK:
        entry = _PENDING.get(flow_id)
        result = entry["result"] if entry else None

    if entry is None:
        return {"status": "expired"}
    if result is None:
        return {"status": "pending"}

    if "access_token" in result:
        # Token cache was already updated by the worker (shared object);
        # we just need to record who's signed in so current_user() works.
        sid = _ensure_sid()
        state = _state_for(sid)
        claims = result.get("id_token_claims") or {}
        state.user = {
            "name": claims.get("name") or claims.get("preferred_username") or "Signed-in user",
            "preferred_username": claims.get("preferred_username"),
            "oid": claims.get("oid"),
        }
        with _PENDING_LOCK:
            _PENDING.pop(flow_id, None)
        session.pop("device_flow_id", None)
        return {"status": "success"}

    err = result.get("error_description") or result.get("error") or "Sign-in failed."
    with _PENDING_LOCK:
        _PENDING.pop(flow_id, None)
    session.pop("device_flow_id", None)
    return {"status": "error", "error": err}


def open_in_system_browser(url: str) -> bool:
    """Open URL in the user's default browser (not the pywebview window).

    Only Microsoft sign-in hosts are allowed, so a malicious page on the
    local port can't trick the app into launching arbitrary URLs.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in _BROWSER_OPEN_ALLOWLIST:
        return False
    try:
        return webbrowser.open(url, new=2)
    except Exception:
        return False


def get_access_token() -> str | None:
    """Return a valid Graph access token, refreshing silently if possible."""
    sid = session.get("sid")
    if not sid:
        return None
    with _SESSIONS_LOCK:
        state = _SESSIONS.get(sid)
    if state is None or state.user is None:
        return None

    msal_app = _build_msal_app(state.cache)
    accounts = msal_app.get_accounts()
    if not accounts:
        return None
    cfg = current_app.config["KFC_CONFIG"]
    result = msal_app.acquire_token_silent(cfg.scopes, account=accounts[0])
    if result and "access_token" in result:
        return result["access_token"]
    return None


def current_user() -> dict | None:
    sid = session.get("sid")
    if not sid:
        return None
    with _SESSIONS_LOCK:
        state = _SESSIONS.get(sid)
    return state.user if state else None


def clear_session() -> None:
    sid = session.get("sid")
    flow_id = session.get("device_flow_id")
    session.clear()
    if sid:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(sid, None)
    if flow_id:
        with _PENDING_LOCK:
            _PENDING.pop(flow_id, None)


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user() or not get_access_token():
            session["post_login_redirect"] = request.url
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped
