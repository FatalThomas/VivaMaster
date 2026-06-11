"""Web entry point. Run with: python app.py"""
from __future__ import annotations

from kfc_entra.updates import start_background_check
from kfc_entra.web import create_app


def main() -> None:
    start_background_check()
    app = create_app()
    port = app.config["KFC_CONFIG"].port
    # host=localhost only - this app holds an Entra admin token and is not
    # designed to be exposed beyond the local machine.
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
