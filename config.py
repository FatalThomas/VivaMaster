"""Configuration loader. Reads from environment variables (and `.env` if present).

Zero-config by default: the app signs in as the user via Microsoft's
pre-consented first-party "Microsoft Azure CLI" public client, requesting
Directory.AccessAsUser.All. That scope means the app can only ever do what
the signed-in user's own Entra roles allow - no app registration and no
tenant-wide admin consent required. Set CLIENT_ID/TENANT_ID to use your own
app registration instead.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Microsoft's first-party "Microsoft Azure CLI" public client. Pre-authorized
# in every tenant for delegated Graph access as the signed-in user, so no
# consent prompt is ever shown. Conditional Access policies still apply.
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


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
        # Loopback redirect at the root path. Azure AD ignores the port for
        # localhost redirects, so this matches the first-party client's
        # registered "http://localhost" URI. Custom app registrations must
        # register http://localhost:<PORT> (path "/").
        return f"http://localhost:{self.port}"

    @property
    def scopes(self) -> list[str]:
        # Delegated "act as the signed-in user" permission. Effective access
        # is the intersection of this scope and the user's own Entra roles
        # (Guest Inviter, User Administrator, Groups Administrator, ...).
        return ["Directory.AccessAsUser.All"]


def load_config() -> Config:
    return Config(
        client_id=os.environ.get("CLIENT_ID", "").strip() or AZURE_CLI_CLIENT_ID,
        # "organizations" lets any work/school account sign in; their home
        # tenant is used. Pin TENANT_ID to restrict sign-in to one tenant.
        tenant_id=os.environ.get("TENANT_ID", "").strip() or "organizations",
        # Only protects the local session cookie. An ephemeral key just means
        # you re-sign-in after an app restart, which is fine for a local tool.
        flask_secret_key=os.environ.get("FLASK_SECRET_KEY", "").strip()
        or secrets.token_hex(32),
        invite_redirect_url=os.environ.get(
            "INVITE_REDIRECT_URL", "https://myapps.microsoft.com"
        ),
        port=int(os.environ.get("PORT", "5000")),
    )
