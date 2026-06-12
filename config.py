"""Configuration loader. Reads from environment variables (and `.env` if present).

Zero-config by default: the app signs in as the user via Microsoft's
pre-consented first-party "Microsoft Azure CLI" public client with the
OAuth 2.0 device code flow. The user types a short code into
microsoft.com/devicelogin - no redirect URIs, no app registration, no
admin consent. Effective access is whatever the signed-in user's own
Entra roles allow. Set CLIENT_ID/TENANT_ID to use your own app
registration instead.
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
    # True when the user pasted their own Azure AD app's Client ID in
    # Settings (or via the CLIENT_ID env var). Drives the scope list.
    is_custom_client: bool = False

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def scopes(self) -> list[str]:
        # Delegated "act as the signed-in user" permissions. Effective access
        # is the intersection of these scopes and the user's own Entra roles
        # (Guest Inviter, User Administrator, Groups Administrator, ...).
        #
        # Default (Azure CLI client): just Directory.AccessAsUser.All, the
        # only Graph scope Microsoft pre-authorizes for that public client.
        # Asking for Files.ReadWrite / Sites.ReadWrite.All on the Azure CLI
        # client fails with AADSTS65002.
        #
        # Custom client: the user has registered their own Azure AD app and
        # ticked the file scopes there, so we can request all three at
        # sign-in - which is what unlocks the OneDrive / SharePoint cloud
        # sync of the mapping files.
        if self.is_custom_client:
            return [
                "Directory.AccessAsUser.All",
                "Files.ReadWrite",
                "Sites.ReadWrite.All",
            ]
        return ["Directory.AccessAsUser.All"]


def load_config() -> Config:
    # Override priority: env vars -> ~/.kfc_entra_manager/app_config.json -> default.
    # Importing inside the function dodges a circular import (kfc_entra.app_config
    # depends on kfc_entra.mappings, which is fine - but config.py is loaded by
    # kfc_entra.web at app-start, before the package is fully importable in some
    # test paths).
    try:
        from kfc_entra.app_config import load as load_app_config
        file_cfg = load_app_config()
    except Exception:  # noqa: BLE001 - never block startup on a bad config file
        file_cfg = {"client_id": "", "tenant_id": ""}

    client_id = (
        os.environ.get("CLIENT_ID", "").strip()
        or file_cfg.get("client_id", "")
        or AZURE_CLI_CLIENT_ID
    )
    tenant_id = (
        os.environ.get("TENANT_ID", "").strip()
        or file_cfg.get("tenant_id", "")
        or "organizations"
    )

    return Config(
        client_id=client_id,
        tenant_id=tenant_id,
        # Only protects the local session cookie. An ephemeral key just means
        # you re-sign-in after an app restart, which is fine for a local tool.
        flask_secret_key=os.environ.get("FLASK_SECRET_KEY", "").strip()
        or secrets.token_hex(32),
        invite_redirect_url=os.environ.get(
            "INVITE_REDIRECT_URL", "https://myapps.microsoft.com"
        ),
        port=int(os.environ.get("PORT", "5000")),
        is_custom_client=client_id != AZURE_CLI_CLIENT_ID,
    )
