"""Background update checker against GitHub Releases.

On startup the app spawns a daemon thread that asks GitHub for the latest
release of this repo and compares it to the running version. The result is
cached on disk for a day so the exe doesn't hit GitHub on every launch, and
a banner with the download link is shown in the UI when a newer release
exists. Everything is best-effort: no network, a private repo without
access, or an API error simply means "no banner" - never a crash or a
startup delay.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import requests

from .mappings import config_dir
from .version import __version__

REPO = "FatalThomas/VivaMaster"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY_SECONDS = 24 * 3600
REQUEST_TIMEOUT = 6


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    exe_download_url: str | None


_lock = threading.Lock()
_available: UpdateInfo | None = None
_thread_started = False


def _cache_path():
    return config_dir() / "update_check.json"


def _parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Non-numeric parts terminate the tuple."""
    parts: list[int] = []
    for piece in text.strip().lstrip("vV").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _is_newer(candidate: str, current: str) -> bool:
    cand, curr = _parse_version(candidate), _parse_version(current)
    return bool(cand) and cand > curr


def _load_cache() -> dict | None:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "checked_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _save_cache(data: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def _fetch_latest() -> UpdateInfo | None:
    resp = requests.get(
        LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    release = resp.json()
    tag = (release.get("tag_name") or "").strip()
    if not tag:
        return None
    exe_url = None
    for asset in release.get("assets") or []:
        if (asset.get("name") or "").lower().endswith(".exe"):
            exe_url = asset.get("browser_download_url")
            break
    return UpdateInfo(
        latest_version=tag.lstrip("vV"),
        release_url=release.get("html_url") or f"https://github.com/{REPO}/releases",
        exe_download_url=exe_url,
    )


def _check() -> None:
    global _available

    cache = _load_cache()
    if cache:
        try:
            checked_at = datetime.fromisoformat(cache["checked_at"])
            age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        except (KeyError, ValueError):
            age = CHECK_EVERY_SECONDS + 1
        if age < CHECK_EVERY_SECONDS and cache.get("latest"):
            info = UpdateInfo(**cache["latest"])
            if _is_newer(info.latest_version, __version__):
                with _lock:
                    _available = info
            return

    try:
        info = _fetch_latest()
    except requests.RequestException:
        return
    if info is None:
        return

    _save_cache(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "latest": asdict(info),
        }
    )
    if _is_newer(info.latest_version, __version__):
        with _lock:
            _available = info


def start_background_check() -> None:
    """Kick off the once-per-launch update check. Safe to call repeatedly."""
    global _thread_started
    with _lock:
        if _thread_started:
            return
        _thread_started = True
    threading.Thread(target=_check, name="update-check", daemon=True).start()


def get_available_update() -> UpdateInfo | None:
    """The update found by the background check, or None."""
    with _lock:
        return _available
