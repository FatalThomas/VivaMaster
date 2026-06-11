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
class GraphGroup:
    id: str
    display_name: str
    description: str | None
    mail_nickname: str | None

    @classmethod
    def from_api(cls, data: dict) -> "GraphGroup":
        return cls(
            id=data.get("id", ""),
            display_name=data.get("displayName") or "",
            description=data.get("description"),
            mail_nickname=data.get("mailNickname"),
        )


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

    # ---------- paged helpers ----------
    def _collect_paged(
        self,
        path: str,
        params: dict | None = None,
        extra_headers: dict | None = None,
        max_items: int = 5000,
    ) -> list[dict]:
        """Follow @odata.nextLink until exhausted (or max_items reached)."""
        headers = dict(self._headers)
        if extra_headers:
            headers.update(extra_headers)
        url = f"{GRAPH_BASE}{path}"
        items: list[dict] = []
        while url and len(items) < max_items:
            resp = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                    message = payload.get("error", {}).get("message", resp.text)
                except ValueError:
                    payload = None
                    message = resp.text
                raise GraphError(resp.status_code, message, payload)
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query string
        return items[:max_items]

    def list_users_by_type(self, user_type: str | None = None) -> list[GraphUser]:
        """List all users, optionally filtered by userType (e.g. 'Guest').

        Filtering on userType is an "advanced query" in Graph, so it needs
        ConsistencyLevel: eventual plus $count=true.
        """
        params: dict[str, Any] = {
            "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            "$top": 999,
        }
        extra_headers: dict | None = None
        if user_type:
            params["$filter"] = f"userType eq '{user_type}'"
            params["$count"] = "true"
            extra_headers = {"ConsistencyLevel": "eventual"}
        raw = self._collect_paged("/users", params=params, extra_headers=extra_headers)
        return [GraphUser.from_api(u) for u in raw]

    def get_user_by_email(self, email: str) -> GraphUser | None:
        """Find a user by mail / UPN / otherMails. Returns None when not found."""
        safe = email.replace("'", "''")
        params = {
            "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            "$filter": (
                f"mail eq '{safe}' or userPrincipalName eq '{safe}' "
                f"or otherMails/any(c:c eq '{safe}')"
            ),
            "$count": "true",
            "$top": 2,
        }
        raw = self._collect_paged(
            "/users", params=params, extra_headers={"ConsistencyLevel": "eventual"}
        )
        return GraphUser.from_api(raw[0]) if raw else None

    # ---------- groups ----------
    def list_groups(self, search: str | None = None) -> list[GraphGroup]:
        # Listing /groups with $orderby (and optionally $filter) is an
        # "advanced query" in Microsoft Graph: it requires both
        # ConsistencyLevel: eventual and $count=true. Without them the
        # request fails with 400 "Invalid search request" / "Request_*".
        params: dict[str, Any] = {
            "$select": "id,displayName,description,mailNickname",
            "$top": 999,
            "$count": "true",
            "$orderby": "displayName",
        }
        if search:
            safe = search.replace("'", "''")
            params["$filter"] = f"startswith(displayName, '{safe}')"
        try:
            raw = self._collect_paged(
                "/groups",
                params=params,
                extra_headers={"ConsistencyLevel": "eventual"},
            )
        except GraphError:
            # Fallback for tenants that block advanced queries: do the
            # simplest possible list call and sort / filter client-side.
            simple = {
                "$select": "id,displayName,description,mailNickname",
                "$top": 999,
            }
            raw = self._collect_paged("/groups", params=simple)
            if search:
                term = search.lower()
                raw = [
                    g
                    for g in raw
                    if (g.get("displayName") or "").lower().startswith(term)
                ]
            raw.sort(key=lambda g: (g.get("displayName") or "").lower())
        return [GraphGroup.from_api(g) for g in raw]

    def create_group(self, display_name: str, description: str | None = None) -> GraphGroup:
        """Create a Microsoft 365 (Unified) group.

        Viva Engage communities and Teams sit on top of Microsoft 365
        groups, so this is what KFC actually needs - not a plain security
        group. The signed-in user needs the Groups Administrator role (or
        an equivalent role / tenant setting that permits M365 group
        creation) for this to succeed.
        """
        from .groups import sanitise_mail_nickname  # local import avoids cycle

        body = {
            "displayName": display_name,
            "description": description or "Created by KFC Entra User Manager",
            "mailEnabled": True,
            "securityEnabled": False,
            "mailNickname": sanitise_mail_nickname(display_name),
            "groupTypes": ["Unified"],
        }
        resp = self._request("POST", "/groups", json=body)
        return GraphGroup.from_api(resp.json())

    def add_member_to_group(self, group_id: str, user_id: str) -> str:
        """Add a user to a group. Returns 'added' or 'already_member'.

        Graph answers 400 "One or more added object references already exist"
        when the user is already in the group - we treat that as idempotent
        success.
        """
        body = {
            "@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}",
        }
        try:
            self._request("POST", f"/groups/{group_id}/members/$ref", json=body)
            return "added"
        except GraphError as exc:
            if exc.status == 400 and "already exist" in (exc.message or "").lower():
                return "already_member"
            raise
