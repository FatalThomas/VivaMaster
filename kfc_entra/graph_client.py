"""Thin wrapper around the Microsoft Graph REST API for user operations."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    # B2B invite tracking: "PendingAcceptance" | "Accepted" | None (for
    # non-Guest accounts). Used by the Re-invites flow to spot stale
    # invitations that need re-sending.
    external_user_state: str | None = None
    # Secondary email addresses on the user object. For B2B Guests the
    # original invitedUserEmailAddress is usually here, even when the
    # primary `mail` field is empty.
    other_mails: list[str] = field(default_factory=list)

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
            external_user_state=data.get("externalUserState"),
            other_mails=list(data.get("otherMails") or []),
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

        # Auto-retry on Microsoft Graph throttling. /$batch sub-requests
        # carry their own per-row 429s (handled by each batch_* helper);
        # this is the outer-request 429, which happens when the tenant
        # hits an overall rate limit. Honour Retry-After (seconds), with
        # exponential fallback when the header's missing or absurd.
        max_429_retries = 6
        attempt = 0
        while True:
            response = requests.request(
                method,
                url,
                headers=self._headers,
                timeout=DEFAULT_TIMEOUT,
                **kwargs,
            )
            if response.status_code != 429 or attempt >= max_429_retries:
                break
            retry_after = response.headers.get("Retry-After", "")
            try:
                wait = max(1, min(60, int(retry_after)))
            except (TypeError, ValueError):
                wait = min(60, 2 ** attempt)
            import time as _time
            _time.sleep(wait)
            attempt += 1

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

    def batch_get_users_by_email(
        self, emails: list[str]
    ) -> dict[str, GraphUser | None]:
        """Resolve up to 20 emails to GraphUsers in ONE Graph $batch round-trip.

        Each sub-request is a /users?$filter=... query identical to
        get_user_by_email, so the same mail / userPrincipalName / otherMails
        matching applies. Email lookups that come back with no hit map to
        None so the caller can mark the row as "not in Entra".

        20x faster than calling get_user_by_email in a loop for large
        reports - 20k email lookups go from ~70 min sequential to ~3-5 min.
        """
        if not emails:
            return {}
        if len(emails) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        from urllib.parse import quote
        select = "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime,externalUserState"
        requests_body = []
        for idx, email in enumerate(emails):
            safe = email.replace("'", "''")
            filter_str = (
                f"mail eq '{safe}' or userPrincipalName eq '{safe}' "
                f"or otherMails/any(c:c eq '{safe}')"
            )
            url = (
                "/users?$filter=" + quote(filter_str, safe="")
                + "&$select=" + quote(select, safe=",")
                + "&$count=true&$top=2"
            )
            requests_body.append({
                "id": str(idx),
                "method": "GET",
                "url": url,
                "headers": {"ConsistencyLevel": "eventual"},
            })

        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, GraphUser | None] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(emails):
                continue
            email = emails[idx]
            status = entry.get("status", 500)
            if 200 <= status < 300:
                body = entry.get("body") or {}
                values = body.get("value") if isinstance(body, dict) else None
                results[email] = (
                    GraphUser.from_api(values[0]) if values else None
                )
            else:
                results[email] = None
        for email in emails:
            results.setdefault(email, None)
        return results

    def list_pending_acceptance_users(self) -> list[GraphUser]:
        """Return every tenant user whose B2B invitation is still pending.

        Paginates ``/users?$filter=externalUserState eq 'PendingAcceptance'``
        with the advanced-query headers (ConsistencyLevel: eventual +
        $count=true) so the filter is accepted by Graph. Used by the
        Re-invites page's "scan tenant" path - no CSV upload required.
        """
        from urllib.parse import urlencode

        select = (
            "id,displayName,userPrincipalName,mail,otherMails,"
            "userType,accountEnabled,createdDateTime,externalUserState"
        )
        params = {
            "$filter": "externalUserState eq 'PendingAcceptance'",
            "$select": select,
            "$count": "true",
            "$top": "100",
        }
        headers = dict(self._headers)
        headers["ConsistencyLevel"] = "eventual"

        url = f"{GRAPH_BASE}/users?{urlencode(params)}"
        out: list[GraphUser] = []
        while url:
            resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                    message = payload.get("error", {}).get("message", resp.text)
                except ValueError:
                    payload = None
                    message = resp.text
                raise GraphError(resp.status_code, message, payload)
            data = resp.json()
            for u in data.get("value", []):
                out.append(GraphUser.from_api(u))
            url = data.get("@odata.nextLink")
        return out

    def batch_invite_guests(
        self,
        invitations: list[dict],
    ) -> dict[str, tuple[str | None, str]]:
        """Send up to 20 Guest invitations in one Graph $batch round-trip.

        ``invitations`` is a list of dicts with keys:

          * ``email``                - the external email address to invite
          * ``display_name``         - the Entra display name to assign
          * ``redirect_url``         - where to land the user after consent
          * ``send_invitation_message`` - whether Microsoft mails the user

        Returns ``{email: (user_id_or_None, error_msg)}``. On success the
        user_id is set and error_msg is "" (or "already_member" if the
        Graph response indicates the address already exists as a Member).
        On failure user_id is None and error_msg carries Graph's reason
        so the caller can surface it.
        """
        if not invitations:
            return {}
        if len(invitations) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = []
        for idx, inv in enumerate(invitations):
            requests_body.append({
                "id": str(idx),
                "method": "POST",
                "url": "/invitations",
                "headers": {"Content-Type": "application/json"},
                "body": {
                    "invitedUserEmailAddress": inv["email"],
                    "invitedUserDisplayName": inv.get("display_name") or inv["email"],
                    "inviteRedirectUrl": inv.get("redirect_url")
                        or "https://myapps.microsoft.com",
                    "sendInvitationMessage": bool(inv.get("send_invitation_message", True)),
                },
            })

        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, tuple[str | None, str]] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(invitations):
                continue
            email = invitations[idx]["email"]
            status = entry.get("status", 500)
            body = entry.get("body") or {}
            if 200 <= status < 300:
                invited_user = (body or {}).get("invitedUser") or {}
                uid = invited_user.get("id")
                results[email] = (uid, "")
            else:
                error = body.get("error") if isinstance(body, dict) else None
                msg = error.get("message") if isinstance(error, dict) else str(body)
                results[email] = (None, msg or f"HTTP {status}")
        for inv in invitations:
            results.setdefault(inv["email"], (None, "No batch response."))
        return results

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

    def batch_add_members_to_group(
        self, group_id: str, user_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Add up to 20 users to a group via Graph's $batch endpoint.

        Returns {user_id: (outcome, reason)} where outcome is one of
        'added' / 'already_member' / 'failed'. Bundling 20 add-to-group
        calls into one HTTP round-trip is roughly 20x faster than calling
        add_member_to_group() in a loop.
        """
        if not user_ids:
            return {}
        if len(user_ids) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = [
            {
                "id": str(idx),
                "method": "POST",
                "url": f"/groups/{group_id}/members/$ref",
                "headers": {"Content-Type": "application/json"},
                "body": {"@odata.id": f"{GRAPH_BASE}/directoryObjects/{uid}"},
            }
            for idx, uid in enumerate(user_ids)
        ]
        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, tuple[str, str]] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(user_ids):
                continue
            uid = user_ids[idx]
            status = entry.get("status", 500)
            if 200 <= status < 300 or status == 204:
                results[uid] = ("added", "")
                continue
            body = entry.get("body") or {}
            msg = (body.get("error") or {}).get("message", str(body))
            if status == 400 and "already exist" in msg.lower():
                results[uid] = ("already_member", "")
            else:
                results[uid] = ("failed", msg)
        # Any user id we didn't see a response for is a Graph oddity - treat
        # as failed so the caller surfaces it instead of silently dropping.
        for uid in user_ids:
            results.setdefault(uid, ("failed", "Graph $batch returned no response for this user."))
        return results

    def list_group_members(self, group_id: str) -> list[GraphUser]:
        """Return every user-type member of a group (paged).

        Uses the OData cast `microsoft.graph.user` so Graph returns only
        user members, not nested groups or service principals - which
        matters when computing "who's in the group but not in the report".
        """
        params: dict[str, Any] = {
            "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            "$top": 999,
        }
        raw = self._collect_paged(
            f"/groups/{group_id}/members/microsoft.graph.user",
            params=params,
        )
        return [GraphUser.from_api(u) for u in raw]

    def list_user_groups(self, user_id: str) -> list[GraphGroup]:
        """Return every group the user is a direct member of.

        Uses the OData cast `microsoft.graph.group` against /memberOf so
        Graph returns only groups (skipping directory roles and other
        directory objects). Non-transitive: we want the user's *direct*
        memberships only, which is how the report flow and the per-user
        chip list both work.
        """
        params: dict[str, Any] = {
            "$select": "id,displayName,description,mailNickname",
            "$top": 999,
        }
        raw = self._collect_paged(
            f"/users/{user_id}/memberOf/microsoft.graph.group",
            params=params,
        )
        return [GraphGroup.from_api(g) for g in raw]

    def batch_remove_members_from_group(
        self, group_id: str, user_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Remove up to 20 users from a group via Graph $batch.

        Returns {user_id: (outcome, reason)} where outcome is one of
        'removed' / 'not_in_group' / 'failed'.
        """
        if not user_ids:
            return {}
        if len(user_ids) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = [
            {
                "id": str(idx),
                "method": "DELETE",
                "url": f"/groups/{group_id}/members/{uid}/$ref",
            }
            for idx, uid in enumerate(user_ids)
        ]
        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, tuple[str, str]] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(user_ids):
                continue
            uid = user_ids[idx]
            status = entry.get("status", 500)
            if 200 <= status < 300 or status == 204:
                results[uid] = ("removed", "")
                continue
            body = entry.get("body") or {}
            msg = (body.get("error") or {}).get("message", str(body))
            lower = msg.lower()
            if status == 404 or "does not exist" in lower or "could not be found" in lower:
                results[uid] = ("not_in_group", "")
            else:
                results[uid] = ("failed", msg)
        for uid in user_ids:
            results.setdefault(uid, ("failed", "Graph $batch returned no response for this user."))
        return results

    # ---------- group owners (Viva Engage "community admins") ----------------
    def list_group_owners(self, group_id: str) -> list[GraphUser]:
        """Return every user owner of a group (paged). Filters to users only."""
        params: dict[str, Any] = {
            "$select": "id,displayName,userPrincipalName,mail,userType,accountEnabled,createdDateTime",
            "$top": 999,
        }
        raw = self._collect_paged(
            f"/groups/{group_id}/owners/microsoft.graph.user",
            params=params,
        )
        return [GraphUser.from_api(u) for u in raw]

    def add_owner_to_group(self, group_id: str, user_id: str) -> str:
        """Promote a single user to group owner. Returns 'added' or 'already_owner'."""
        body = {"@odata.id": f"{GRAPH_BASE}/users/{user_id}"}
        try:
            self._request("POST", f"/groups/{group_id}/owners/$ref", json=body)
            return "added"
        except GraphError as exc:
            if exc.status == 400 and "already exist" in (exc.message or "").lower():
                return "already_owner"
            raise

    def batch_add_owners_to_group(
        self, group_id: str, user_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Add up to 20 users as owners via Graph $batch.

        Returns {user_id: (outcome, reason)} - 'added' / 'already_owner' /
        'failed'. Mirrors batch_add_members_to_group exactly except the
        endpoint is /owners/$ref instead of /members/$ref.
        """
        if not user_ids:
            return {}
        if len(user_ids) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = [
            {
                "id": str(idx),
                "method": "POST",
                "url": f"/groups/{group_id}/owners/$ref",
                "headers": {"Content-Type": "application/json"},
                "body": {"@odata.id": f"{GRAPH_BASE}/users/{uid}"},
            }
            for idx, uid in enumerate(user_ids)
        ]
        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, tuple[str, str]] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(user_ids):
                continue
            uid = user_ids[idx]
            status = entry.get("status", 500)
            if 200 <= status < 300 or status == 204:
                results[uid] = ("added", "")
                continue
            body = entry.get("body") or {}
            msg = (body.get("error") or {}).get("message", str(body))
            if status == 400 and "already exist" in msg.lower():
                results[uid] = ("already_owner", "")
            else:
                results[uid] = ("failed", msg)
        for uid in user_ids:
            results.setdefault(uid, ("failed", "Graph $batch returned no response for this user."))
        return results

    def remove_owner_from_group(self, group_id: str, user_id: str) -> str:
        """Demote a user from group owner. Returns 'removed' or 'not_an_owner'."""
        try:
            self._request("DELETE", f"/groups/{group_id}/owners/{user_id}/$ref")
            return "removed"
        except GraphError as exc:
            lower = (exc.message or "").lower()
            if exc.status == 404 or "does not exist" in lower or "could not be found" in lower:
                return "not_an_owner"
            raise

    def batch_count_group_owners(self, group_ids: list[str]) -> dict[str, int]:
        """Return {group_id: owner_count} for up to 20 groups in one $batch.

        Used by the Groups page to drive the "has admins" filter without
        listing every owner of every group. Each sub-request hits
        /groups/{id}/owners/$count with ConsistencyLevel: eventual; the
        response body for that endpoint is a plain integer, which we
        parse out of the $batch wrapper.
        """
        if not group_ids:
            return {}
        if len(group_ids) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = [
            {
                "id": str(idx),
                "method": "GET",
                "url": f"/groups/{gid}/owners/$count",
                "headers": {"ConsistencyLevel": "eventual"},
            }
            for idx, gid in enumerate(group_ids)
        ]
        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, int] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(group_ids):
                continue
            gid = group_ids[idx]
            status = entry.get("status", 500)
            if not 200 <= status < 300:
                results[gid] = 0
                continue
            body = entry.get("body")
            try:
                results[gid] = int(body) if body is not None else 0
            except (TypeError, ValueError):
                # Some Graph builds wrap the count in {"value": N}.
                if isinstance(body, dict) and "value" in body:
                    try:
                        results[gid] = int(body["value"])
                    except (TypeError, ValueError):
                        results[gid] = 0
                else:
                    results[gid] = 0
        for gid in group_ids:
            results.setdefault(gid, 0)
        return results


        """Return every group the user is a direct member of (paged).

        Uses the `microsoft.graph.group` cast so directory roles and other
        directory-object memberships are filtered out at the source.
        """
        params: dict[str, Any] = {
            "$select": "id,displayName,description,mailNickname",
            "$top": 999,
        }
        raw = self._collect_paged(
            f"/users/{user_id}/memberOf/microsoft.graph.group",
            params=params,
        )
        return [GraphGroup.from_api(g) for g in raw]

    def remove_member_from_group(self, group_id: str, user_id: str) -> str:
        """Remove one user from one group. Returns 'removed' or 'not_in_group'."""
        try:
            self._request("DELETE", f"/groups/{group_id}/members/{user_id}/$ref")
            return "removed"
        except GraphError as exc:
            lower = (exc.message or "").lower()
            if exc.status == 404 or "does not exist" in lower or "could not be found" in lower:
                return "not_in_group"
            raise

    def delete_user(self, user_id: str) -> str:
        """Move one user to Deleted Users (soft-delete; recoverable for 30 days).

        Returns 'deleted' or 'already_gone' (404 - they were already deleted).
        """
        try:
            self._request("DELETE", f"/users/{user_id}")
            return "deleted"
        except GraphError as exc:
            lower = (exc.message or "").lower()
            if exc.status == 404 or "does not exist" in lower or "could not be found" in lower:
                return "already_gone"
            raise

    def batch_delete_users(self, user_ids: list[str]) -> dict[str, tuple[str, str]]:
        """Delete up to 20 users via Graph $batch (DELETE /users/{id}).

        Returns {user_id: (outcome, reason)} where outcome is one of
        'deleted' / 'already_gone' / 'failed'. Deleted users land in the
        tenant's Deleted Users container for 30 days, so this is
        recoverable - we still surface a clear "deleted" outcome
        because the user is no longer findable through normal queries.
        """
        if not user_ids:
            return {}
        if len(user_ids) > 20:
            raise ValueError("Microsoft Graph $batch is limited to 20 requests.")

        requests_body = [
            {"id": str(idx), "method": "DELETE", "url": f"/users/{uid}"}
            for idx, uid in enumerate(user_ids)
        ]
        resp = self._request("POST", "/$batch", json={"requests": requests_body})
        results: dict[str, tuple[str, str]] = {}
        for entry in resp.json().get("responses") or []:
            try:
                idx = int(entry.get("id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(user_ids):
                continue
            uid = user_ids[idx]
            status = entry.get("status", 500)
            if 200 <= status < 300 or status == 204:
                results[uid] = ("deleted", "")
                continue
            body = entry.get("body") or {}
            msg = (body.get("error") or {}).get("message", str(body))
            lower = msg.lower()
            if status == 404 or "does not exist" in lower or "could not be found" in lower:
                results[uid] = ("already_gone", "")
            else:
                results[uid] = ("failed", msg)
        for uid in user_ids:
            results.setdefault(uid, ("failed", "Graph $batch returned no response for this user."))
        return results

    def disable_user(self, user_id: str) -> None:
        """Soft-delete: set accountEnabled to false. User is preserved in Entra."""
        self.update_user(user_id, {"accountEnabled": False})

    # ---------- file storage (OneDrive / SharePoint) ----------
    def me(self) -> dict:
        """Return the signed-in user object (id, displayName, mail, etc.)."""
        return self._request("GET", "/me", params={"$select": "id,displayName,mail,userPrincipalName"}).json()

    def get_file_text(self, drive_path: str) -> str | None:
        """Read a small file from a Graph drive path. Returns None if missing.

        drive_path is the full path under GRAPH_BASE, e.g.
        /me/drive/root:/Apps/KFCEntraManager/mappings.json:/content
        or /sites/{site-id}/drive/root:/Apps/KFCEntraManager/mappings.json:/content
        """
        try:
            resp = self._request("GET", drive_path)
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise
        return resp.text

    def put_file_text(self, drive_path: str, content: str) -> dict:
        """Create or overwrite a file at the given drive path. Parent folders
        in the path are created on demand by the upload endpoint."""
        url = drive_path if drive_path.startswith("http") else f"{GRAPH_BASE}{drive_path}"
        headers = dict(self._headers)
        headers["Content-Type"] = "text/plain"
        resp = requests.put(
            url,
            headers=headers,
            data=content.encode("utf-8"),
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
        return resp.json()

    def resolve_sharepoint_site(self, site_url: str) -> dict:
        """Resolve a SharePoint site URL to its Graph site object.

        Accepts forms like:
          https://contoso.sharepoint.com/sites/KFC-Network
          contoso.sharepoint.com:/sites/KFC-Network
        Returns the {"id", "displayName", "webUrl", ...} site object.
        """
        from urllib.parse import urlparse
        u = site_url.strip()
        if u.startswith("http://") or u.startswith("https://"):
            parsed = urlparse(u)
            host = parsed.netloc
            path = parsed.path.rstrip("/")
        else:
            # contoso.sharepoint.com:/sites/foo form
            if ":/" in u:
                host, path = u.split(":/", 1)
                path = "/" + path.lstrip("/")
            else:
                host = u
                path = ""
        if not host:
            raise GraphError(400, "Could not parse SharePoint site URL.")
        path_segment = f":{path}" if path else ""
        resp = self._request("GET", f"/sites/{host}{path_segment}")
        return resp.json()

