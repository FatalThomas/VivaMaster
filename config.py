"""Configuration loader. Reads from environment variables (and `.env` if present).

Zero-config by default: the app signs in as the user via Microsoft's
pre-consented first-party "Microsoft Azure CLI" public client with the
OAuth 2.0 device code flow. The user types a short code into
microsoft.com/devicelogin - no redirect URIs, no app registration, no
admin consent. Effective access is whatever the signed-in user's own
Entra roles allow. Set CLIENT_ID/TENANT_ID env vars (or
kfc_entra/app_config.json) to point at your own app registration if you
need broader Graph scopes than Directory.AccessAsUser.All.
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
    # URL of the small license server the app POSTs ``/verify`` to on
    # launch. Unset => trial-only, no key can be validated. See
    # tools/license_server/server.py for a deployable reference.
    license_server_url: str = ""
    # Storefront link the "Buy a license" button on the gating page
    # points at (Gumroad / Stripe / etc).
    license_buy_url: str = ""

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def scopes(self) -> list[str]:
        # Delegated "act as the signed-in user" permission. Sign-in stays
        # one-click because the app registration already has admin consent
        # for this scope. Effective directory access is its intersection
        # with the signed-in user's own Entra roles.
        #
        # Cloud-sync scopes (Files.ReadWrite, Sites.ReadWrite.All) are NOT
        # requested at sign-in because Sites.ReadWrite.All requires admin
        # consent on the app registration and Files.ReadWrite often does
        # too under tenant consent policies - and a missing grant 500s the
        # whole device flow. Cloud sync re-acquires its own token using
        # incremental consent once admin has clicked "Grant admin consent"
        # on the app registration (not yet wired up; the Settings page
        # surfaces the requirement).
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
    # "organizations" lets any work/school tenant sign in - which is fine
    # because the Azure CLI client is multi-tenant and pre-authed in all of
    # them. Pin TENANT_ID if you switch to a single-tenant custom app.
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
        license_server_url=os.environ.get(
            "LICENSE_SERVER_URL",
            "https://licenses.vivaui.com",
        ).strip(),
        license_buy_url=os.environ.get(
            "LICENSE_BUY_URL",
            "https://buy.stripe.com/dRm4gy9h0eg06zkd19cQU01",
        ),
    )
