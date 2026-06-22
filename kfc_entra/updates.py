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
import os
import platform
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .mappings import config_dir
from .version import __version__

REPO = "FatalThomas/VivaMaster"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
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
    """Hit GitHub Releases once per launch and update _available accordingly.

    The disk cache is still written (it's useful for diagnostics) but the
    freshness gate is gone: every launch checks GitHub. Network errors
    fall back to whatever was cached last time so the banner can still
    appear while the user is briefly offline.
    """
    global _available

    try:
        info = _fetch_latest()
    except requests.RequestException:
        info = None

    if info is None:
        # Network failure - fall back to the previous result on disk so
        # we still show the banner when the user is briefly offline.
        cache = _load_cache()
        if cache and cache.get("latest"):
            info = UpdateInfo(**cache["latest"])
        else:
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
    """Kick off the once-per-launch update check. Safe to call repeatedly.

    Also opportunistically cleans up the .old file from a previous update
    swap - the prior session couldn't delete it because it was still the
    running process at the time.
    """
    cleanup_old_exe()
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


# ---------- one-click self-update (Windows exe builds only) ----------
#
# Earlier versions used a .bat/.vbs script that waited for the old exe
# to exit, copied the new one into place, paused for Defender, then
# launched the new exe. Two problems:
#
#   1. Even at 30s the Defender pause occasionally raced PyInstaller's
#      single-file bootloader extracting python311.dll, producing a
#      "Failed to load Python DLL" popup the moment the new exe ran.
#   2. The script needed cmd.exe or wscript.exe, both of which flash
#      console windows briefly on some Windows configurations.
#
# We now use the much simpler "atomic-swap then exit" pattern.
# Windows allows renaming a running .exe on the same volume (the
# process keeps its file handle), so we:
#
#   1. Rename the currently-running exe to a .old sibling.
#   2. Move the freshly-downloaded exe into the original path.
#   3. Exit the running process.
#
# The user's taskbar / Start Menu shortcut still points at the
# original path - which now resolves to the new exe - so reopening
# the app picks up the update. The pause between "I clicked Update"
# and "I clicked the taskbar icon" is plenty for Defender to finish.
# The .old leftover gets deleted on the next launch.


_BACKUP_SUFFIX = ".old"


def is_frozen() -> bool:
    """True when running as a PyInstaller-built exe."""
    return bool(getattr(sys, "frozen", False))


def self_install_supported() -> bool:
    return is_frozen() and platform.system() == "Windows"


def _backup_path_for(exe: Path) -> Path:
    """Path the old exe is renamed to during an update swap."""
    return exe.with_name(exe.stem + _BACKUP_SUFFIX + exe.suffix)


def cleanup_old_exe() -> None:
    """Delete the .old sibling left behind by the previous update.

    Called once on app startup. Best-effort: any failure is silently
    ignored (the file will simply linger and we'll try again next
    launch).
    """
    if not self_install_supported():
        return
    backup = _backup_path_for(Path(sys.executable))
    if backup.exists():
        try:
            backup.unlink()
        except OSError:
            pass


def download_update(info: UpdateInfo) -> Path:
    """Download the release exe into the per-user config dir.

    Downloads to a .partial file first and verifies the byte count against
    Content-Length before moving it into place, so a dropped connection can
    never leave a truncated exe where the updater would pick it up.
    """
    if not info.exe_download_url:
        raise RuntimeError("This release has no exe attached.")
    updates_dir = config_dir() / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    target = updates_dir / f"KFC Entra User Manager-{info.latest_version}.exe"
    partial = target.with_suffix(".partial")

    with requests.get(
        info.exe_download_url, stream=True, timeout=(10, 30), allow_redirects=True
    ) as resp:
        resp.raise_for_status()
        expected = int(resp.headers.get("Content-Length") or 0)
        written = 0
        with open(partial, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                written += len(chunk)

    if expected and written != expected:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"Download incomplete ({written} of {expected} bytes) - try again."
        )
    os.replace(partial, target)
    return target


def install_update_and_restart(new_exe: Path) -> None:
    """Atomic-swap the running exe with the freshly-downloaded one, then exit.

    Sequence: rename the running exe out of the way, move the new exe
    into the original path, exit. The user reopens via their existing
    shortcut / taskbar icon - no script, no Defender race, no popup.

    The threading.Timer gives Flask a beat to flush the HTTP response
    telling the UI "update installed" before the process dies.
    """
    target_exe = Path(sys.executable)
    backup_exe = _backup_path_for(target_exe)

    # If a prior update left a .old behind (we couldn't delete it while
    # it was still running), clean it now before reusing the name.
    if backup_exe.exists():
        try:
            backup_exe.unlink()
        except OSError:
            # Worst case: rename below will fail and we'll surface the
            # error; the user falls back to a manual reinstall.
            pass

    # 1. Rename the running exe. Windows lets you rename an in-use file
    #    on the same volume - the process keeps working through its
    #    existing file handle.
    os.rename(target_exe, backup_exe)
    try:
        # 2. Move the new exe into the original path. os.replace is
        #    atomic on Windows when source and destination are on the
        #    same volume.
        os.replace(new_exe, target_exe)
    except OSError:
        # Best-effort rollback so the user isn't left with no exe at
        # the expected path.
        os.replace(backup_exe, target_exe)
        raise

    # 3. Exit so any browser tabs / pywebview windows close, and so the
    #    user knows the update finished. They reopen at the new version
    #    via their taskbar shortcut.
    threading.Timer(1.0, os._exit, args=(0,)).start()
