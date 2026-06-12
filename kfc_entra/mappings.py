"""Persistent Franchisee / Store -> Entra Group mappings.

Stored as JSON in a per-user config dir:
  - Windows:      %APPDATA%\\KFCEntraManager\\
  - macOS/Linux:  ~/.kfc_entra_manager/

Franchisee mappings (uppercase codes like "ANR") live in mappings.json.
Store mappings (verbatim store names like "Forster") live in
store_mappings.json - the report's STORE column is the key, the Entra
group id and name are the value.
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


def _path(filename: str) -> Path:
    return config_dir() / filename


def _load(filename: str) -> dict:
    path = _path(filename)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("mappings"), dict):
            raise ValueError("bad shape")
        data.setdefault("version", _SCHEMA_VERSION)
        return data
    except (OSError, ValueError):
        return {"version": _SCHEMA_VERSION, "mappings": {}}


def _write(filename: str, data: dict) -> None:
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


def _normalise_franchisee(code: str) -> str:
    return code.strip().upper()


def _normalise_store(name: str) -> str:
    # Stores are not upper-cased - "Forster" and "Salamander Bay" are read
    # verbatim from the report and concatenated with "KFC " to form the
    # group name. Just trim whitespace.
    return name.strip()


# ---------- Franchisee mappings (existing API; unchanged behaviour) ----------
def load_mappings() -> dict:
    return _load("mappings.json")


def save_mapping(code: str, group_id: str, group_name: str) -> dict:
    code = _normalise_franchisee(code)
    data = load_mappings()
    entry = {
        "group_id": group_id,
        "group_name": group_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["mappings"][code] = entry
    _write("mappings.json", data)
    return entry


def delete_mapping(code: str) -> bool:
    code = _normalise_franchisee(code)
    data = load_mappings()
    if code in data["mappings"]:
        del data["mappings"][code]
        _write("mappings.json", data)
        return True
    return False


# ---------- Store mappings (new for the multi-mode report apply) ------------
def load_store_mappings() -> dict:
    return _load("store_mappings.json")


def save_store_mapping(store: str, group_id: str, group_name: str) -> dict:
    store = _normalise_store(store)
    data = load_store_mappings()
    entry = {
        "group_id": group_id,
        "group_name": group_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data["mappings"][store] = entry
    _write("store_mappings.json", data)
    return entry


def delete_store_mapping(store: str) -> bool:
    store = _normalise_store(store)
    data = load_store_mappings()
    if store in data["mappings"]:
        del data["mappings"][store]
        _write("store_mappings.json", data)
        return True
    return False
