"""Sample license server for the KFC Entra User Manager paywall.

Deploy this anywhere that can answer HTTP - a Cloudflare Worker, a tiny
VPS, fly.io, Render, an Azure Function. The app POSTs to ``/verify``
with::

    {
      "key": "KFC-XXXX-XXXX-XXXX",
      "tenant_id": "<entra tenant guid>",
      "machine_id": "<sha256 prefix>",
      "app_version": "1.0.54"
    }

and expects back::

    {
      "ok": true,
      "expires_at": "2027-06-15T00:00:00+00:00",
      "edition": "pro",
      "message": "Valid - thanks!"
    }

Keys live in a JSON file alongside this script (``keys.json``) so it's
easy to issue / revoke licenses without redeploying. Example::

    {
      "KFC-1234-5678-ABCD": {
        "tenant_id": "a48b58d3-4ae6-4310-bd85-016f3555e958",
        "expires_at": "2027-06-15T00:00:00+00:00",
        "edition": "pro",
        "revoked": false,
        "note": "Yum! Australia - Tom Fisher"
      }
    }

Set ``tenant_id`` to ``"*"`` to issue a key that works in any tenant.
Set ``revoked: true`` to kill a key without deleting the row (the
caller sees a clear "revoked" message).

Run locally for testing::

    pip install flask
    python server.py            # listens on :5001
    LICENSE_SERVER_URL=http://localhost:5001 python ../../desktop.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

KEYS_PATH = Path(__file__).with_name("keys.json")


def _load_keys() -> dict:
    try:
        with open(KEYS_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


@app.route("/verify", methods=["POST"])
def verify():
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    tenant_id = (body.get("tenant_id") or "").strip()
    if not key:
        return jsonify(ok=False, message="No key provided.")

    keys = _load_keys()
    entry = keys.get(key)
    if not entry:
        return jsonify(ok=False, message="Unknown license key.")
    if entry.get("revoked"):
        return jsonify(ok=False, message="This license has been revoked.")
    allowed_tenant = entry.get("tenant_id", "*")
    if allowed_tenant != "*" and allowed_tenant != tenant_id:
        return jsonify(
            ok=False,
            message="This license is not authorized for your tenant.",
        )
    expires_at = entry.get("expires_at")
    expires = _parse(expires_at)
    if expires and expires < datetime.now(timezone.utc):
        return jsonify(
            ok=False,
            expires_at=expires_at,
            edition=entry.get("edition", "pro"),
            message="This license has expired.",
        )
    return jsonify(
        ok=True,
        expires_at=expires_at,
        edition=entry.get("edition", "pro"),
        message=entry.get("note") or "Valid.",
    )


@app.route("/health")
def health():
    return jsonify(ok=True, keys=len(_load_keys()))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
