"""MSAL-based authentication using OAuth 2.0 device code flow.

No redirect URI, no browser callback, no chance of redirect loops. The
signed-in admin sees a short code, types it into microsoft.com/devicelogin
(in their default browser, which we offer to open for them), and we
background-poll MSAL for the resulting tokens.

Pending flows live in an in-process dict, keyed by a UUID we put in the
Flask session. The app only listens on 127.0.0.1, the dict only holds
short-lived flow state, and each entry expires after 15 minutes.
"""
from __future__ import annotations

import threading
import time
import webbrowser
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

# Pending device flows: {flow_id: {"flow", "started", "result", "cache"}}.
_PENDING: dict[str, dict] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 15 * 60


def _build_msal_app(cache: msal.SerializableTokenCache | None = None) -> msal.PublicClientApplication:
    cfg = current_app.config["KFC_CONFIG"]
    return msal.PublicClientApplication(cfg.client_id, authority=cfg.authority, token_cache=cache)


def _purge_expired_unlocked() -> None:
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["started"] > _TTL_SECONDS]:
        _PENDING.pop(k, None)


def _flow_info(flow_id: str, flow: dict) -> dict:
    verification_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
    user_code = flow["user_code"]
    return {
        "flow_id": flow_id,
        "user_code": user_code,
        "verification_uri": verification_uri,
        # Microsoft accepts ?otc=CODE on the devicelogin page to pre-fill the
        # code, so the user just signs in - no copy/paste required.
        "verification_uri_complete": f"{verification_uri}?otc={user_code}",
        "message": flow.get("message", ""),
        "expires_in": flow.get("expires_in", 900),
    }


def start_device_flow() -> dict:
    """Initiate device flow and spawn the background MSAL poller.

    Idempotent: if the session already has a pending (not yet completed)
    device flow with plenty of time left, that flow's code is reused
    instead of starting a new one. This way re-hitting /login - whether
    from a button, a refresh, or pywebview navigating away and back -
    never silently invalidates the code the user just pasted into
    Microsoft.
    """
    cfg = current_app.config["KFC_CONFIG"]

    existing_id = session.get("device_flow_id")
    if existing_id:
        with _LOCK:
            entry = _PENDING.get(existing_id)
            if entry is not None and entry["result"] is None:
                age = time.time() - entry["started"]
                # Keep a 60s buffer so we don't hand back a code about to expire.
                if age < _TTL_SECONDS - 60:
                    return _flow_info(existing_id, entry["flow"])

    msal_app = _build_msal_app()
    flow = msal_app.initiate_device_flow(scopes=cfg.scopes)
    if "user_code" not in flow:
        raise RuntimeError(
            flow.get("error_description") or flow.get("error") or "Could not start device flow."
        )

    flow_id = uuid4().hex
    client_id, authority, scopes = cfg.client_id, cfg.authority, list(cfg.scopes)

    def worker(flow_copy: dict) -> None:
        # MSAL's acquire_token_by_device_flow blocks until success, expiry,
        # or user denial. We let it block on this background thread.
        cache = msal.SerializableTokenCache()
        local = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
        try:
            result = local.acquire_token_by_device_flow(flow_copy)
        except Exception as exc:  # network glitch, etc.
            result = {"error": "worker_failed", "error_description": str(exc)}
        with _LOCK:
            entry = _PENDING.get(flow_id)
            if entry is not None:
                entry["result"] = result
                entry["cache"] = cache.serialize() if cache.has_state_changed else None

    with _LOCK:
        _purge_expired_unlocked()
        _PENDING[flow_id] = {
            "flow": flow,
            "started": time.time(),
            "result": None,
            "cache": None,
        }
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
    with _LOCK:
        entry = _PENDING.get(flow_id)
        result = entry["result"] if entry else None
        cache_blob = entry["cache"] if entry else None

    if entry is None:
        return {"status": "expired"}
    if result is None:
        return {"status": "pending"}

    # Result available - persist tokens into the Flask session and clean up.
    if "access_token" in result:
        if cache_blob:
            session["token_cache"] = cache_blob
        session["user"] = result.get("id_token_claims", {})
        with _LOCK:
            _PENDING.pop(flow_id, None)
        session.pop("device_flow_id", None)
        return {"status": "success"}

    err = result.get("error_description") or result.get("error") or "Sign-in failed."
    with _LOCK:
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
    cache = msal.SerializableTokenCache()
    raw = session.get("token_cache")
    if raw:
        cache.deserialize(raw)
    msal_app = _build_msal_app(cache)
    accounts = msal_app.get_accounts()
    if not accounts:
        return None
    cfg = current_app.config["KFC_CONFIG"]
    result = msal_app.acquire_token_silent(cfg.scopes, account=accounts[0])
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()
    if result and "access_token" in result:
        return result["access_token"]
    return None


def current_user() -> dict | None:
    return session.get("user")


def clear_session() -> None:
    flow_id = session.get("device_flow_id")
    session.clear()
    if flow_id:
        with _LOCK:
            _PENDING.pop(flow_id, None)


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user() or not get_access_token():
            session["post_login_redirect"] = request.url
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped
