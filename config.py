"""Configuration loader. Reads from environment variables (and `.env` if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


@dataclass(frozen=True)
class Config:
    client_id: str
    tenant_id: str
    flask_secret_key: str
    invite_redirect_url: str
    port: int

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/auth/callback"

    @property
    def scopes(self) -> list[str]:
        # Delegated Microsoft Graph permissions the signed-in admin consents to.
        return [
            "User.Invite.All",
            "User.ReadWrite.All",
            "Directory.ReadWrite.All",
        ]


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return value


def load_config() -> Config:
    return Config(
        client_id=_require("CLIENT_ID"),
        tenant_id=_require("TENANT_ID"),
        flask_secret_key=_require("FLASK_SECRET_KEY"),
        invite_redirect_url=os.environ.get(
            "INVITE_REDIRECT_URL", "https://myapps.microsoft.com"
        ),
        port=int(os.environ.get("PORT", "5000")),
    )
