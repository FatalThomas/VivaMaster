"""High-level user operations used by the web routes."""
from __future__ import annotations

from dataclasses import dataclass

from .graph_client import GraphClient, GraphError, GraphUser


@dataclass
class InviteResult:
    user_id: str
    display_name: str
    email: str
    converted_to_member: bool
    warning: str | None = None


def invite_and_promote(
    client: GraphClient,
    email: str,
    display_name: str,
    invite_redirect_url: str,
    send_invitation_message: bool = True,
) -> InviteResult:
    """Invite an external email as a guest, then flip them to Member.

    The two-step operation is wrapped here so the route handler stays thin.
    If the promotion to Member fails, the guest still exists - we surface that
    as a warning so the admin can retry from the user list.
    """
    invitation = client.invite_guest(
        email=email,
        display_name=display_name,
        redirect_url=invite_redirect_url,
        send_invitation_message=send_invitation_message,
    )
    invited_user = invitation.get("invitedUser") or {}
    user_id = invited_user.get("id")
    if not user_id:
        raise GraphError(500, "Invitation succeeded but no user ID was returned.", invitation)

    warning: str | None = None
    converted = False
    try:
        client.convert_to_member(user_id)
        converted = True
    except GraphError as exc:
        warning = (
            f"Invitation sent, but converting to Member failed: {exc.message}. "
            "You can retry from the user list."
        )

    return InviteResult(
        user_id=user_id,
        display_name=display_name,
        email=email,
        converted_to_member=converted,
        warning=warning,
    )


def list_users_sorted(client: GraphClient, search: str | None = None) -> list[GraphUser]:
    return client.list_users(search=search)
