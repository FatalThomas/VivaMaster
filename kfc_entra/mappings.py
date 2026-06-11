"""Persistent Franchisee -> Entra Group mappings.

Stored as JSON in a per-user config dir:
  - Windows:      %APPDATA%\\KFCEntraManager\\mappings.json
  - macOS/Linux:  ~/.kfc_entra_manager/mappings.json
"""
from __future__ import annotations

import json
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_VERSION = 1


def config_dir() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "KFCEntraManager"
    return Path.home() / ".kfc_entra_manager"


def _mappings_path() -> Path:
    return config_dir() / "mappings.json"


def load_mappings() -> dict:
    """Return {"version": 1, "mappings": {CODE: {...}}}. Never raises."""
    path = _mappings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("mappings"), dict):
            raise ValueError("bad shape")
        data.setdefault("version", _SCHEMA_VERSION)
        return data
    except (OSError, ValueError):
        return {"version": _SCHEMA_VERSION, "mappings": {}}


def _write(data: dict) -> None:
    path = _mappings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file in the same dir, then replace.
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


def save_mapping(code: str, group_id: str, group_name: str) -> dict:
    """Upsert one mapping and return the saved entry."""
    code = code.strip().upper()
    data = load_mappings()
    entry = {
        "group_id": group_id,
        "group_name": group_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["mappings"][code] = entry
    _write(data)
    return entry


def delete_mapping(code: str) -> bool:
    """Remove a mapping. Returns True when something was deleted."""
    code = code.strip().upper()
    data = load_mappings()
    if code in data["mappings"]:
        del data["mappings"][code]
        _write(data)
        return True
    return False
