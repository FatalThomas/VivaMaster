"""License gating: 14-day trial, then a remotely-validated license key.

Flow on every app launch:

  1. ``load_state()`` reads the per-user ``license.json`` next to the
     mapping files. First-ever launch stamps ``first_launch_at`` and
     starts a 14-day trial.
  2. ``current_entitlement()`` decides what to show:
       - "trial"             - still inside the trial window; no key needed
       - "licensed"          - server confirmed the key (cached for 24h)
       - "server_unreachable"- couldn't reach the server, within 7-day grace
       - "expired"           - trial over OR server says the key is dead
       - "no_license"        - trial over and no key entered yet
  3. ``is_unlocked()`` returns True for "trial" / "licensed" /
     "server_unreachable". Anything else and the app routes the user to
     the licence page instead of the sign-in flow.

The server protocol is intentionally tiny so a Cloudflare Worker or a
five-line Flask app can implement it (sample in ``tools/license_server``).
POST a JSON body of ``{key, tenant_id, machine_id, app_version}`` to
``LICENSE_SERVER_URL`` and respond with
``{ok: bool, expires_at: ISO, edition: str, message: str}``.

Local file state never has to be edited by hand - the Settings page
exposes the key field + "check now" button - but it's plain JSON, so a
support engineer can fix a stuck install by deleting the file.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .mappings import config_dir

LICENSE_FILE = "license.json"
TRIAL_DAYS = 14
RECHECK_INTERVAL = timedelta(hours=24)
SERVER_UNREACHABLE_GRACE = timedelta(days=7)
DEFAULT_TIMEOUT = 10  # seconds


# ---------------- state ----------------


@dataclass
class LicenseState:
    """In-memory snapshot of license.json + a verdict for the UI.

    ``state``: "trial" | "licensed" | "expired" | "no_license" |
               "server_unreachable" | "invalid"
    ``can_use``: True when the app should let the user in.
    ``buy_url``: where the "Buy a license" button points.
    """

    state: str = "no_license"
    key: str = ""
    first_launch_at: str = ""
    expires_at: str = ""  # ISO 8601
    last_check_at: str = ""
    edition: str = ""
    message: str = ""
    can_use: bool = False
    days_remaining: int | None = None
    buy_url: str = ""

    def to_disk(self) -> dict:
        # Drop UI-only fields before persisting.
        d = asdict(self)
        for ephemeral in ("can_use", "message", "days_remaining", "buy_url"):
            d.pop(ephemeral, None)
        return d


def _path() -> Path:
    return config_dir() / LICENSE_FILE


def _read_raw() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_raw(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _machine_id() -> str:
    """Stable per-machine identifier (not PII): SHA-256 of the MAC + OS info."""
    raw = f"{uuid.getnode()}|{platform.node()}|{platform.system()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------- trial config (admin-controlled) ----------------

TRIAL_CONFIG_RECHECK = timedelta(minutes=30)


def _fetch_trial_config(server_url: str) -> dict | None:
    """GET /trial-config on the licence server. Returns the parsed dict, or
    None on any network / JSON failure (caller falls back to the cached
    or default values)."""
    if not server_url:
        return None
    try:
        resp = requests.get(
            server_url.rstrip("/") + "/trial-config",
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json() or {}
    except (requests.RequestException, ValueError):
        return None
    if not data.get("ok", True):
        return None
    return {
        "enabled": bool(data.get("enabled", True)),
        "ends_at": data.get("ends_at") or "",
        # Stripe Payment Link (or any URL) the "Buy a license" button on
        # the desktop app should point at. Empty string = fall back to
        # the LICENSE_BUY_URL env var / config.py default.
        "buy_url": (data.get("buy_url") or "").strip(),
        "fetched_at": _iso(_now()),
    }


def _cached_trial_config(data: dict, server_url: str, force: bool = False) -> dict:
    """Return the trial config either from license.json's cache (still
    fresh) or from a fresh /trial-config call. Defaults to "enabled,
    no global end-date" when neither is available.

    When ``force=True``, the disk cache is bypassed and a fresh fetch is
    attempted - used on app launch so toggling the banner in /admin
    takes effect as soon as the customer restarts the app instead of
    waiting out the 30-minute cache.
    """
    cached = data.get("trial_config") or {}
    last = _parse(cached.get("fetched_at", ""))
    if not force and last and _now() - last < TRIAL_CONFIG_RECHECK:
        return cached
    fresh = _fetch_trial_config(server_url)
    if fresh:
        save_state({"trial_config": fresh})
        return fresh
    # Either no server URL or the call failed - keep using the cache if
    # we have one, otherwise fall back to defaults.
    if cached:
        return cached
    return {"enabled": True, "ends_at": "", "fetched_at": ""}


# ---------------- core API ----------------


def load_state() -> dict:
    """Read license.json, seeding a trial start-time on first ever launch."""
    data = _read_raw()
    if "first_launch_at" not in data:
        data["first_launch_at"] = _iso(_now())
        _write_raw(data)
    return data


def save_state(updates: dict) -> dict:
    """Merge ``updates`` into license.json and return the new full dict."""
    data = _read_raw()
    data.update(updates)
    _write_raw(data)
    return data


def clear_key() -> None:
    """Remove the stored key (e.g. for support purposes). Trial-start date
    is preserved so an old install can't game the trial by clearing keys."""
    data = _read_raw()
    for k in ("key", "expires_at", "last_check_at", "edition"):
        data.pop(k, None)
    _write_raw(data)


def _trial_state(
    first_launch_iso: str,
    buy_url: str,
    trial_config: dict | None = None,
) -> LicenseState:
    """Compute the trial verdict, honouring the admin-controlled remote
    config when present:

      * ``trial_config.enabled == False`` -> trial is disabled globally,
        the install is treated as expired so it lands on the licence
        page immediately.
      * ``trial_config.ends_at`` set -> every install rolls over on that
        same global date (good for a public-beta cutoff).
      * ``trial_config.ends_at`` blank -> fall back to a per-install
        14-day clock from ``first_launch_at``.
    """
    cfg = trial_config or {}
    if cfg and cfg.get("enabled") is False:
        return LicenseState(
            state="expired",
            first_launch_at=first_launch_iso,
            expires_at="",
            can_use=False,
            days_remaining=0,
            message=(
                "Free trial is disabled. Enter a license key to continue."
            ),
            buy_url=buy_url,
        )

    started = _parse(first_launch_iso) or _now()
    # Remote ends_at wins when set; otherwise per-install + 14 days.
    remote_ends = _parse(cfg.get("ends_at", "")) if cfg else None
    expires = remote_ends or (started + timedelta(days=TRIAL_DAYS))
    now = _now()
    remaining = (expires - now).days
    if now < expires:
        return LicenseState(
            state="trial",
            first_launch_at=first_launch_iso,
            expires_at=_iso(expires),
            can_use=True,
            days_remaining=max(0, remaining),
            message=f"Free trial - {remaining} day(s) remaining.",
            buy_url=buy_url,
        )
    return LicenseState(
        state="expired",
        first_launch_at=first_launch_iso,
        expires_at=_iso(expires),
        can_use=False,
        days_remaining=0,
        message=(
            "Your free trial has ended. Enter a license key to continue."
            if remote_ends
            else f"Your {TRIAL_DAYS}-day free trial has ended. "
                  "Enter a license key to continue."
        ),
        buy_url=buy_url,
    )


def _verify_against_server(
    server_url: str,
    key: str,
    tenant_id: str,
    app_version: str,
) -> tuple[dict, str]:
    """POST the key to the license server. Returns ``(payload, error)`` -
    on success the payload has ``ok``, ``expires_at``, etc; on transport
    failure the payload is empty and ``error`` carries the reason."""
    try:
        resp = requests.post(
            server_url.rstrip("/") + "/verify",
            json={
                "key": key,
                "tenant_id": tenant_id,
                "machine_id": _machine_id(),
                "app_version": app_version,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        return {}, str(exc) or "Network error"
    try:
        return resp.json() or {}, ""
    except ValueError:
        return {}, f"License server returned non-JSON (HTTP {resp.status_code})."


def current_entitlement(
    server_url: str,
    tenant_id: str,
    app_version: str,
    buy_url: str = "",
    force_recheck: bool = False,
) -> LicenseState:
    """Decide if the app should unlock for this launch.

    Order:
      1. Honour the cached server verdict if it's < 24h old.
      2. If a key is stored, call the license server.
      3. Otherwise fall back to the trial logic.

    A 5xx / network failure within the 7-day grace returns
    "server_unreachable" + can_use=True so a temporary outage doesn't
    lock the user out.
    """
    data = load_state()
    key = (data.get("key") or "").strip()
    first_launch = data.get("first_launch_at") or _iso(_now())

    # Pick up the admin-controlled trial config (enable flag + global
    # end-date + Stripe payment link) once per probe. Remote buy_url
    # wins over the config.py default so the storefront URL can be
    # changed without re-shipping the exe.
    trial_cfg = _cached_trial_config(data, server_url, force=force_recheck)
    if trial_cfg.get("buy_url"):
        buy_url = trial_cfg["buy_url"]

    # No key entered -> trial only.
    if not key:
        return _trial_state(first_launch, buy_url, trial_config=trial_cfg)

    # We have a key. Use the cached verdict if recent and force_recheck
    # is False - skips a Graph round-trip on every page load.
    last_check = _parse(data.get("last_check_at", ""))
    cached_expires = _parse(data.get("expires_at", ""))
    cached_edition = data.get("edition") or "licensed"
    now = _now()
    if (
        not force_recheck
        and last_check
        and now - last_check < RECHECK_INTERVAL
        and cached_expires
    ):
        if now < cached_expires:
            return LicenseState(
                state="licensed",
                key=key,
                first_launch_at=first_launch,
                expires_at=_iso(cached_expires),
                last_check_at=_iso(last_check),
                edition=cached_edition,
                can_use=True,
                message=f"Licensed ({cached_edition}) - valid until {_iso(cached_expires)}.",
                days_remaining=(cached_expires - now).days,
                buy_url=buy_url,
            )
        # Cached verdict says expired - re-check to be sure.

    # No server URL set: we can't validate. Treat as unlicensed but allow
    # the trial path if it's still active.
    if not server_url:
        trial = _trial_state(first_launch, buy_url, trial_config=trial_cfg)
        if trial.can_use:
            trial.message = (
                "License server not configured (LICENSE_SERVER_URL). "
                "Falling back to trial."
            )
        else:
            trial.message = (
                "License server not configured and your trial has ended. "
                "Set LICENSE_SERVER_URL or contact support."
            )
        return trial

    # Hit the server.
    payload, err = _verify_against_server(server_url, key, tenant_id, app_version)
    if err:
        # Honour the grace window.
        if last_check and now - last_check < SERVER_UNREACHABLE_GRACE and cached_expires and now < cached_expires:
            return LicenseState(
                state="server_unreachable",
                key=key,
                first_launch_at=first_launch,
                expires_at=_iso(cached_expires),
                last_check_at=_iso(last_check),
                edition=cached_edition,
                can_use=True,
                message=(
                    f"License server unreachable ({err}). Using the "
                    "cached verdict - retrying tomorrow."
                ),
                days_remaining=(cached_expires - now).days,
                buy_url=buy_url,
            )
        # Outside grace: lock.
        return LicenseState(
            state="server_unreachable",
            key=key,
            first_launch_at=first_launch,
            expires_at=data.get("expires_at", ""),
            edition=cached_edition,
            can_use=False,
            message=(
                "Could not reach the license server in the past 7 days "
                f"({err}). Sign-in is paused until the server is "
                "reachable again."
            ),
            buy_url=buy_url,
        )

    ok = bool(payload.get("ok"))
    reason = (payload.get("reason") or "").strip().lower()
    expires_iso = payload.get("expires_at") or ""
    edition = payload.get("edition") or "licensed"
    server_msg = payload.get("message") or ""

    # If the server doesn't recognise the key at all (admin deleted it,
    # or the customer typed garbage), the install is no different from a
    # fresh download - clear the stored key and run the trial logic so
    # the user lands on the trial / "Activate" page rather than a
    # confusing "License rejected" lock screen.
    if not ok and reason == "unknown":
        clear_key()
        trial_cfg = _cached_trial_config(_read_raw(), server_url, force=force_recheck)
        if trial_cfg.get("buy_url"):
            buy_url = trial_cfg["buy_url"]
        return _trial_state(first_launch, buy_url, trial_config=trial_cfg)

    if ok:
        save_state({
            "key": key,
            "expires_at": expires_iso,
            "edition": edition,
            "last_check_at": _iso(now),
        })
        expires_dt = _parse(expires_iso)
        days_remaining = (expires_dt - now).days if expires_dt else None
        return LicenseState(
            state="licensed",
            key=key,
            first_launch_at=first_launch,
            expires_at=expires_iso,
            last_check_at=_iso(now),
            edition=edition,
            can_use=True,
            message=server_msg or f"Licensed ({edition}).",
            days_remaining=days_remaining,
            buy_url=buy_url,
        )

    # Server explicitly rejected the key. Expired -> show the dedicated
    # "your license has expired, renew" page; everything else (revoked,
    # tenant mismatch, machine mismatch, corrupted) -> the generic
    # "license rejected" lock screen with the server's message.
    save_state({
        "last_check_at": _iso(now),
        "expires_at": expires_iso,
        "edition": edition,
    })
    rejection_state = "expired" if reason == "expired" else "invalid"
    return LicenseState(
        state=rejection_state,
        key=key,
        first_launch_at=first_launch,
        expires_at=expires_iso,
        last_check_at=_iso(now),
        edition=edition,
        can_use=False,
        message=server_msg or "License server rejected this key.",
        buy_url=buy_url,
    )


def is_unlocked(state: LicenseState) -> bool:
    return bool(state.can_use)
