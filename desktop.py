"""Desktop entry point. Launches the Flask app in a background thread and
wraps it in a native pywebview window."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import webview

from kfc_entra.updates import start_background_check
from kfc_entra.web import create_app


class DesktopApi:
    """JS-callable bridge exposed as ``window.pywebview.api`` in the WebView.

    Currently only used to surface a native save-as dialog for CSV
    downloads - WebView2 + pywebview swallows the default browser
    download flow, so we hand the file to Python and write it ourselves.
    """

    def save_csv(self, filename: str, content: str) -> dict:
        """Show a save-as dialog and write ``content`` to the chosen path.

        Returns ``{"ok": True, "path": "..."}`` on success,
        ``{"ok": False, "cancelled": True}`` if the user cancelled, or
        ``{"ok": False, "error": "..."}`` on a write failure.
        """
        safe = (filename or "download.csv").strip() or "download.csv"
        if not safe.lower().endswith(".csv"):
            safe = safe + ".csv"
        try:
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return {"ok": False, "error": "No active window."}
            # pywebview's save dialog returns the absolute path (or None
            # / empty if the user cancelled). file_types lets the user
            # change the extension; default to the requested filename.
            result = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=safe,
                file_types=("CSV files (*.csv)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            # Some pywebview versions return a tuple/list, others a str.
            path = result[0] if isinstance(result, (list, tuple)) else result
            Path(path).write_text(content, encoding="utf-8", newline="")
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def _serve(app, port: int) -> None:
    # Use Flask's built-in WSGI server; we're only ever serving one local user.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def _wait_until_ready(port: int, timeout: float = 8.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    start_background_check()
    app = create_app()
    port = app.config["KFC_CONFIG"].port

    server_thread = threading.Thread(target=_serve, args=(app, port), daemon=True)
    server_thread.start()

    if not _wait_until_ready(port):
        raise RuntimeError(f"Flask server failed to start on port {port}")

    webview.create_window(
        title="KFC Entra User Manager",
        url=f"http://127.0.0.1:{port}",
        width=1200,
        height=820,
        min_size=(900, 600),
        js_api=DesktopApi(),
    )
    webview.start()


if __name__ == "__main__":
    main()
