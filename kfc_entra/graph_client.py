"""Thin wrapper around the Microsoft Graph REST API for user operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_TIMEOUT = 30


class GraphError(Exception):
    """Raised when Graph returns a non-success status."""

    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"Graph API {status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload


@dataclass
class GraphUser:
    id: str
    display_name: str
    user_principal_name: str
    mail: str | None
    user_type: str | None
    account_enabled: bool
    created_date_time: str | None

    @classmethod
    def from_api(cls, data: dict) -> "GraphUser":
        return cls(
            id=data.get("id", ""),
            display_name=data.get("displayName") or "",
            user_principal_name=data.get("userPrincipalName") or "",
            mail=data.get("mail"),
            user_type=data.get("userType"),
            account_enabled=bool(data.get("accountEnabled", True)),
            created_date_time=data.get("createdDateTime"),
        )


class GraphClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ---------- low level ----------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        response = requests.request(
            method,
            url,
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT,
            **kwargs,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("error", {}).get("message", response.text)
            except ValueError:
                payload = None
                message = response.text
            raise GraphError(response.status_code, message, payload)
        return response

    # ---------- users ----------
    def list_users(self, search: str | None = None, top: int = 100) -> list[GraphUser]:
        params: dict[str, Any] = {
            "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            "$top": top,
            "$orderby": "displayName",
        }
        headers = dict(self._headers)
        if search:
            # $search requires ConsistencyLevel: eventual
            params["$search"] = f'"displayName:{search}" OR "mail:{search}" OR "userPrincipalName:{search}"'
            headers["ConsistencyLevel"] = "eventual"
        resp = requests.get(
            f"{GRAPH_BASE}/users",
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                message = payload.get("error", {}).get("message", resp.text)
            except ValueError:
                payload = None
                message = resp.text
            raise GraphError(resp.status_code, message, payload)
        return [GraphUser.from_api(u) for u in resp.json().get("value", [])]

    def get_user(self, user_id: str) -> GraphUser:
        resp = self._request(
            "GET",
            f"/users/{user_id}",
            params={
                "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            },
        )
        return GraphUser.from_api(resp.json())

    def invite_guest(
        self,
        email: str,
        display_name: str,
        redirect_url: str,
        send_invitation_message: bool = True,
    ) -> dict:
        """Invite an external user as a guest. Returns the invitation response
        (which includes `invitedUser.id`)."""
        body = {
            "invitedUserEmailAddress": email,
            "invitedUserDisplayName": display_name,
            "inviteRedirectUrl": redirect_url,
            "sendInvitationMessage": send_invitation_message,
        }
        resp = self._request("POST", "/invitations", json=body)
        return resp.json()

    def update_user(self, user_id: str, patch: dict) -> None:
        self._request("PATCH", f"/users/{user_id}", json=patch)

    def convert_to_member(self, user_id: str) -> None:
        self.update_user(user_id, {"userType": "Member"})

    def set_display_name(self, user_id: str, display_name: str) -> None:
        self.update_user(user_id, {"displayName": display_name})
