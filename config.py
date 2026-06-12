"""Configuration loader. Reads from environment variables (and `.env` if present).

Zero-config by default: the app signs in as the user via KFC's own
public-client Entra app registration with the OAuth 2.0 device code
flow. The user types a short code into microsoft.com/devicelogin and
consents once - no admin consent needed, just the delegated permissions
on the app registration:

  - Directory.AccessAsUser.All (users + groups)
  - Files.ReadWrite            (mapping sync to OneDrive)
  - Sites.ReadWrite.All        (mapping sync to Teams / SharePoint)

Effective directory access is the intersection of those scopes and the
signed-in user's own Entra roles. Set CLIENT_ID / TENANT_ID env vars
(or kfc_entra/app_config.json) to point at a different app registration.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# KFC's public-client Entra app registration. Lives in KFC's tenant, so
# only KFC accounts can sign in. Has all three delegated Graph scopes
# pre-registered with user-level consent - the user sees one consent
# screen on first sign-in, never again.
KFC_APP_CLIENT_ID = "6ae76fa1-f2c9-4b64-b7af-1d0fb59ce17d"

# KFC's Entra tenant ID (yumau.onmicrosoft.com). Required because the app
# registration is single-tenant - using "organizations" or "common" as the
# authority would 401 with AADSTS50059 "no tenant-identifying information".
KFC_TENANT_ID = "a48b58d3-4ae6-4310-bd85-016f3555e958"


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
    def scopes(self) -> list[str]:
        # Delegated "act as the signed-in user" permissions, all three
        # pre-registered on the KFC app and user-consentable. Effective
        # directory access is the intersection of Directory.AccessAsUser.All
        # and the signed-in user's own Entra roles (Guest Inviter, User
        # Administrator, Groups Administrator, ...). The two file scopes
        # unlock the OneDrive / SharePoint cloud sync of mapping files.
        return [
            "Directory.AccessAsUser.All",
            "Files.ReadWrite",
            "Sites.ReadWrite.All",
        ]


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
        or KFC_APP_CLIENT_ID
    )
    tenant_id = (
        os.environ.get("TENANT_ID", "").strip()
        or file_cfg.get("tenant_id", "")
        or KFC_TENANT_ID
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
    )
