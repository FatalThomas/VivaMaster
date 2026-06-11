"""Desktop entry point. Launches the Flask app in a background thread and
wraps it in a native pywebview window."""
from __future__ import annotations

import logging
import threading
import time

import webview

from kfc_entra.updates import start_background_check
from kfc_entra.web import create_app


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
    )
    webview.start()


if __name__ == "__main__":
    main()
