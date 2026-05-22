"""MSAL-based authentication for the signed-in KFC admin.

Uses the OAuth 2.0 Authorization Code flow with PKCE (PublicClientApplication),
storing the MSAL token cache inside the Flask session so refresh tokens work
across requests.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

import msal
from flask import current_app, redirect, request, session, url_for


def _build_msal_app(cache: msal.SerializableTokenCache | None = None) -> msal.PublicClientApplication:
    cfg = current_app.config["KFC_CONFIG"]
    return msal.PublicClientApplication(
        cfg.client_id,
        authority=cfg.authority,
        token_cache=cache,
    )


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    raw = session.get("token_cache")
    if raw:
        cache.deserialize(raw)
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        session["token_cache"] = cache.serialize()


def start_auth_flow() -> str:
    """Begin the auth code flow. Returns the Microsoft login URL to redirect to."""
    cfg = current_app.config["KFC_CONFIG"]
    app = _build_msal_app()
    flow = app.initiate_auth_code_flow(
        scopes=cfg.scopes,
        redirect_uri=cfg.redirect_uri,
    )
    session["auth_flow"] = flow
    return flow["auth_uri"]


def finish_auth_flow(query_params: dict) -> dict:
    """Exchange the auth code for tokens. Returns the parsed result dict."""
    cache = _load_cache()
    app = _build_msal_app(cache)
    flow = session.pop("auth_flow", None)
    if not flow:
        return {"error": "no_flow", "error_description": "Auth flow expired or missing."}
    result = app.acquire_token_by_auth_code_flow(flow, query_params)
    _save_cache(cache)
    if "access_token" in result:
        session["user"] = result.get("id_token_claims", {})
    return result


def get_access_token() -> str | None:
    """Return a valid access token for Graph, refreshing silently if possible."""
    cache = _load_cache()
    app = _build_msal_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None
    cfg = current_app.config["KFC_CONFIG"]
    result = app.acquire_token_silent(cfg.scopes, account=accounts[0])
    _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def current_user() -> dict | None:
    return session.get("user")


def clear_session() -> None:
    session.clear()


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user() or not get_access_token():
            # Remember where they were heading so we can come back after login.
            session["post_login_redirect"] = request.url
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped
