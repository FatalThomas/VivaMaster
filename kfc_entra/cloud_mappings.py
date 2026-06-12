"""Cloud-sync the Franchisee / Store mapping files to Microsoft 365.

The local mappings live in mappings.json / store_mappings.json under the
per-user config dir (see kfc_entra.mappings). For cross-device use we also
push them to either:

  - the signed-in user's personal OneDrive (default), or
  - a SharePoint document library shared with the wider team.

Settings (where to sync, on/off) are persisted in cloud_settings.json
next to the local mapping files - one tiny JSON, edited via /settings.

Sync model
----------
Local is the working copy. On every save_mapping / save_store_mapping call
from a web route, we push the full local file to the cloud (best effort;
network or token failures don't break the local save). When the page is
loaded, we pull the cloud copy and merge it in - for each mapping key the
copy with the later "updated_at" timestamp wins. Local is then written
back so the merged state survives a restart.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .graph_client import GraphClient, GraphError
from .mappings import (
    _path,
    config_dir,
    load_mappings,
    load_store_mappings,
)

# Where in the picked drive the JSON files live. Apps/KFCEntraManager keeps
# things tidy and signals "owned by this tool" to anyone browsing the drive.
FOLDER = "Apps/KFCEntraManager"
MAPPINGS_FILE = "mappings.json"
STORE_MAPPINGS_FILE = "store_mappings.json"

SETTINGS_FILE = "cloud_settings.json"
_SCHEMA_VERSION = 1


# ---------- settings (where to sync) ----------

def _settings_path() -> Path:
    return _path(SETTINGS_FILE)


def load_settings() -> dict:
    """Return the cloud-sync settings, with safe defaults."""
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("bad shape")
    except (OSError, ValueError):
        data = {}
    return {
        "version": _SCHEMA_VERSION,
        "enabled": bool(data.get("enabled", False)),
        # "onedrive" or "sharepoint"
        "destination": data.get("destination") or "onedrive",
        # Only used when destination == "sharepoint"
        "sharepoint_site_url": data.get("sharepoint_site_url") or "",
        "sharepoint_site_id": data.get("sharepoint_site_id") or "",
        "sharepoint_site_name": data.get("sharepoint_site_name") or "",
        # Telemetry for the Settings page
        "last_push_at": data.get("last_push_at") or "",
        "last_pull_at": data.get("last_pull_at") or "",
        "last_error": data.get("last_error") or "",
    }


def save_settings(settings: dict) -> dict:
    """Persist settings, returning the normalised dict."""
    current = load_settings()
    current.update({k: v for k, v in settings.items() if k in current})
    current["version"] = _SCHEMA_VERSION
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return current


def _update_settings(**fields: Any) -> dict:
    current = load_settings()
    current.update(fields)
    return save_settings(current)


# ---------- drive-path resolution ----------

def _drive_path(filename: str, settings: dict) -> str:
    """Return the Graph drive path for the JSON file under the active dest."""
    rel = f"{FOLDER}/{filename}"
    if settings.get("destination") == "sharepoint":
        site_id = settings.get("sharepoint_site_id") or ""
        if not site_id:
            raise ValueError("SharePoint destination selected but no site is configured.")
        return f"/sites/{site_id}/drive/root:/{rel}:/content"
    return f"/me/drive/root:/{rel}:/content"


# ---------- low-level push / pull of one file ----------

def _push_file(client: GraphClient, filename: str, settings: dict) -> None:
    """Upload the local copy of `filename` to the cloud (overwrites)."""
    src = _path(filename)
    if not src.exists():
        # Nothing to push yet - first save will create it.
        return
    with open(src, encoding="utf-8") as fh:
        content = fh.read()
    client.put_file_text(_drive_path(filename, settings), content)


def _pull_file(client: GraphClient, filename: str, settings: dict) -> dict | None:
    """Fetch the cloud copy of `filename`. Returns the parsed JSON dict, or
    None if the file doesn't exist in the cloud yet (first run)."""
    text = client.get_file_text(_drive_path(filename, settings))
    if text is None:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("mappings"), dict):
            return data
    except ValueError:
        pass
    return None


def _merge(local: dict, remote: dict) -> tuple[dict, int]:
    """Merge two mapping dicts; later updated_at wins. Returns (merged, n_changed)."""
    merged = {"version": _SCHEMA_VERSION, "mappings": dict(local.get("mappings") or {})}
    changed = 0
    for key, entry in (remote.get("mappings") or {}).items():
        local_entry = merged["mappings"].get(key)
        if not local_entry:
            merged["mappings"][key] = entry
            changed += 1
            continue
        local_ts = local_entry.get("updated_at") or ""
        remote_ts = entry.get("updated_at") or ""
        if remote_ts > local_ts:
            merged["mappings"][key] = entry
            changed += 1
    return merged, changed


def _write_local(filename: str, data: dict) -> None:
    """Overwrite the on-disk local file (atomic)."""
    path = _path(filename)
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


# ---------- public API used by web routes ----------

def is_enabled() -> bool:
    return bool(load_settings().get("enabled"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def push_all(client: GraphClient) -> dict:
    """Push both mapping files to the configured cloud destination.

    Returns the updated settings dict so the caller can flash a message.
    Raises GraphError / ValueError on hard failures so callers can decide
    whether to surface it.
    """
    settings = load_settings()
    if not settings.get("enabled"):
        return settings
    try:
        _push_file(client, MAPPINGS_FILE, settings)
        _push_file(client, STORE_MAPPINGS_FILE, settings)
    except (GraphError, ValueError) as exc:
        return _update_settings(last_error=str(exc))
    return _update_settings(last_push_at=now_iso(), last_error="")


def pull_all(client: GraphClient) -> tuple[dict, int, int]:
    """Pull both mapping files from the cloud and merge into local.

    Returns (settings, fz_added, store_added) - the counts are the number of
    mapping keys the cloud contributed (added or overwritten because they
    were newer).
    """
    settings = load_settings()
    if not settings.get("enabled"):
        return settings, 0, 0
    try:
        fz_added = _pull_and_merge(client, MAPPINGS_FILE, load_mappings(), settings)
        store_added = _pull_and_merge(
            client, STORE_MAPPINGS_FILE, load_store_mappings(), settings
        )
    except (GraphError, ValueError) as exc:
        updated = _update_settings(last_error=str(exc))
        return updated, 0, 0
    updated = _update_settings(last_pull_at=now_iso(), last_error="")
    return updated, fz_added, store_added


def _pull_and_merge(
    client: GraphClient, filename: str, local: dict, settings: dict
) -> int:
    remote = _pull_file(client, filename, settings)
    if remote is None:
        return 0
    merged, changed = _merge(local, remote)
    if changed:
        _write_local(filename, merged)
    return changed


__all__ = [
    "FOLDER",
    "MAPPINGS_FILE",
    "STORE_MAPPINGS_FILE",
    "is_enabled",
    "load_settings",
    "save_settings",
    "push_all",
    "pull_all",
    "config_dir",
]
