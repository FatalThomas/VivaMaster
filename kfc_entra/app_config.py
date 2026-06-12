"""Per-user override for the Azure AD client_id / tenant_id.

Sign-in defaults to Microsoft's pre-authed "Microsoft Azure CLI" public
client, which is fine for the core Directory.AccessAsUser.All scope but
isn't pre-authorized for Files.ReadWrite / Sites.ReadWrite.All. To use
those (for cloud-syncing the mapping files), the user registers their
own Azure AD app and pastes its Client ID into the Settings page.

That client_id (and the tenant_id, if pinned) lives here, on disk, next
to mappings.json. It's read at app start by config.load_config() with
priority:

  1. CLIENT_ID / TENANT_ID env vars (CI override)
  2. This file (per-user setup)
  3. The Azure CLI defaults

Changes require a restart - msal.PublicClientApplication is built once at
startup with these values, so the Save button flashes "close and reopen"
rather than trying to hot-swap mid-session.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .mappings import config_dir

APP_CONFIG_FILE = "app_config.json"
_SCHEMA_VERSION = 1


def _path() -> Path:
    return config_dir() / APP_CONFIG_FILE


def load() -> dict:
    """Read the saved override, returning a normalised dict (blank by default)."""
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("bad shape")
    except (OSError, ValueError):
        data = {}
    return {
        "version": _SCHEMA_VERSION,
        "client_id": (data.get("client_id") or "").strip(),
        "tenant_id": (data.get("tenant_id") or "").strip(),
        "updated_at": data.get("updated_at") or "",
    }


def save(client_id: str, tenant_id: str) -> dict:
    data = {
        "version": _SCHEMA_VERSION,
        "client_id": (client_id or "").strip(),
        "tenant_id": (tenant_id or "").strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
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
    return data


def clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def is_custom() -> bool:
    return bool(load()["client_id"])
