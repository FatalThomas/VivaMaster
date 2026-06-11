"""Group-related helpers built on top of GraphClient."""
from __future__ import annotations

import re

from .graph_client import GraphClient, GraphError, GraphGroup

# mailNickname: no spaces, no @ () \ [] " ; : <> , and max 64 chars.
_NICKNAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def sanitise_mail_nickname(display_name: str) -> str:
    """Turn an arbitrary display name into a valid Entra mailNickname."""
    nickname = _NICKNAME_BAD.sub("-", display_name.strip())
    nickname = re.sub(r"-{2,}", "-", nickname).strip("-._")
    if not nickname:
        nickname = "kfc-group"
    return nickname[:64]


def add_user_to_group(client: GraphClient, group_id: str, user_id: str) -> str:
    """Add a user to a group, treating "already a member" as success.

    Returns 'added' or 'already_member'. Raises GraphError otherwise.
    """
    return client.add_member_to_group(group_id, user_id)


def ensure_group(
    client: GraphClient,
    group_id: str | None,
    group_name: str,
) -> tuple[GraphGroup, bool]:
    """Return (group, created). Creates the group when no group_id is given.

    When a group_id is supplied we trust it (Graph will 404 on the first
    membership call if it's stale, which the caller surfaces per-row).
    """
    if group_id:
        return (
            GraphGroup(id=group_id, display_name=group_name, description=None, mail_nickname=None),
            False,
        )
    if not group_name.strip():
        raise GraphError(400, "A group name is required to create a new group.")
    created = client.create_group(group_name.strip())
    return created, True
