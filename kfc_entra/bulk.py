"""Bulk orchestration generators.

Each generator yields plain dicts so the SSE routes can do:

    for event in iter_...:
        yield f"data: {json.dumps(event)}\\n\\n"

Event vocabulary (``type`` key):
  phase    - human-readable stage banner ("Finding users...")
  start    - {"total": n, ...} the work list is known
  progress - per-item outcome: {"current", "total", "user", "status", "reason"}
  group    - a Franchisee group was resolved/created during apply
  done     - final summary incl. a ``failures`` list for the CSV download
  error    - fatal, stream is over
"""
from __future__ import annotations

from typing import Iterable, Iterator

from .graph_client import GraphClient, GraphError, GraphUser
from .groups import ensure_group
from .mappings import save_mapping
from .report import EmployeeRow


def _user_label(user: GraphUser) -> str:
    return user.mail or user.user_principal_name or user.display_name or user.id


def iter_convert_to_member(
    client: GraphClient,
    scope: str = "guests",
    user_ids: list[str] | None = None,
) -> Iterator[dict]:
    """Convert users to userType=Member.

    scope:
      "guests"   - everyone whose userType == Guest
      "all"      - everyone whose userType != Member (Guest, null, anything)
      "selected" - the explicit ``user_ids`` list (idempotent: already-Member
                   entries are reported as skipped)
    """
    yield {"type": "phase", "message": "Finding users to convert..."}
    try:
        if user_ids:
            targets: list[GraphUser] = []
            for uid in user_ids:
                try:
                    targets.append(client.get_user(uid))
                except GraphError:
                    targets.append(
                        GraphUser(
                            id=uid, display_name="", user_principal_name=uid,
                            mail=None, user_type=None, account_enabled=True,
                            created_date_time=None,
                        )
                    )
        elif scope == "all":
            targets = [u for u in client.list_users_by_type(None) if u.user_type != "Member"]
        else:
            targets = client.list_users_by_type("Guest")
    except GraphError as exc:
        yield {"type": "error", "message": f"Could not list users: {exc.message}"}
        return

    total = len(targets)
    yield {"type": "start", "total": total, "scope": scope}

    succeeded = failed = skipped = 0
    failures: list[dict] = []
    for i, user in enumerate(targets, start=1):
        label = _user_label(user)
        if user.user_type == "Member":
            skipped += 1
            yield {
                "type": "progress", "current": i, "total": total,
                "user": label, "status": "skipped", "reason": "Already a Member",
            }
            continue
        try:
            client.convert_to_member(user.id)
            succeeded += 1
            yield {
                "type": "progress", "current": i, "total": total,
                "user": label, "status": "ok", "reason": "",
            }
        except GraphError as exc:
            failed += 1
            failures.append({"user": label, "reason": exc.message})
            yield {
                "type": "progress", "current": i, "total": total,
                "user": label, "status": "failed", "reason": exc.message,
            }

    yield {
        "type": "done",
        "summary": {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "failures": failures,
        },
    }


def iter_apply_mappings(
    client: GraphClient,
    parsed_rows: Iterable[EmployeeRow],
    assignments: dict[str, dict],
    include_unknown: bool = False,
) -> Iterator[dict]:
    """Add report rows to their Franchisee's Entra group.

    ``assignments`` maps Franchisee code -> {"group_id": str|None,
    "group_name": str}. Codes mapped to a name without an id get the group
    created first; the new mapping is persisted immediately.
    """
    rows = list(parsed_rows)

    # --- resolve / create groups per Franchisee --------------------------
    yield {"type": "phase", "message": "Resolving Franchisee groups..."}
    resolved: dict[str, dict] = {}
    for code, assignment in assignments.items():
        code = code.strip().upper()
        group_id = (assignment.get("group_id") or "").strip()
        group_name = (assignment.get("group_name") or "").strip()
        if not group_id and not group_name:
            continue  # unmapped: those rows will be reported as skipped
        try:
            group, created = ensure_group(client, group_id or None, group_name)
        except GraphError as exc:
            yield {
                "type": "group", "franchisee": code, "status": "failed",
                "group_name": group_name or group_id, "reason": exc.message,
            }
            continue
        resolved[code] = {"group_id": group.id, "group_name": group.display_name or group_name}
        save_mapping(code, group.id, group.display_name or group_name)
        yield {
            "type": "group", "franchisee": code,
            "status": "created" if created else "resolved",
            "group_id": group.id,
            "group_name": group.display_name or group_name,
        }

    # --- work out the apply set ------------------------------------------
    # Only touch rows whose Franchisee was successfully resolved to a
    # group. Rows in unmapped Franchisees are left completely untouched -
    # we summarise the count once instead of emitting a per-row "skipped"
    # event, which previously made it look like the apply was running
    # against every user in the report.
    work: list[EmployeeRow] = []
    untouched_by_code: dict[str, int] = {}
    for row in rows:
        code = (row.franchisee or "UNK").strip().upper()
        if row.is_unknown and not include_unknown:
            untouched_by_code[code] = untouched_by_code.get(code, 0) + 1
            continue
        if code not in resolved:
            untouched_by_code[code] = untouched_by_code.get(code, 0) + 1
            continue
        work.append(row)

    if untouched_by_code:
        unmapped_codes = sorted(untouched_by_code)
        sample = ", ".join(unmapped_codes[:5])
        if len(unmapped_codes) > 5:
            sample += f", +{len(unmapped_codes) - 5} more"
        yield {
            "type": "phase",
            "message": (
                f"Leaving {sum(untouched_by_code.values())} row(s) from "
                f"{len(unmapped_codes)} unmapped Franchisee(s) untouched: {sample}"
            ),
        }

    total = len(work)
    yield {"type": "start", "total": total}

    per_franchisee: dict[str, dict] = {}
    failures: list[dict] = []
    email_cache: dict[str, str | None] = {}

    def bucket(code: str) -> dict:
        return per_franchisee.setdefault(
            code, {"added": 0, "already_member": 0, "skipped": 0, "failed": 0}
        )

    for i, row in enumerate(work, start=1):
        code = (row.franchisee or "UNK").strip().upper()
        label = row.email or row.name or f"row {row.row_number}"
        stats = bucket(code)
        target = resolved[code]  # guaranteed - we filtered to mapped-only above

        # Resolve the Entra user id: prefer Yammer ID, else email lookup.
        user_id: str | None = None
        if row.in_entra:
            user_id = row.yammer_id.strip()
        elif row.email:
            if row.email in email_cache:
                user_id = email_cache[row.email]
            else:
                try:
                    found = client.get_user_by_email(row.email)
                    user_id = found.id if found else None
                except GraphError:
                    user_id = None
                email_cache[row.email] = user_id

        if not user_id:
            stats["skipped"] += 1
            yield {
                "type": "progress", "current": i, "total": total,
                "franchisee": code, "user": label,
                "status": "skipped", "reason": "Not in Entra yet (no Yammer ID and no match by email)",
            }
            continue

        try:
            outcome = client.add_member_to_group(target["group_id"], user_id)
        except GraphError as exc:
            stats["failed"] += 1
            failures.append({"user": label, "franchisee": code, "reason": exc.message})
            yield {
                "type": "progress", "current": i, "total": total,
                "franchisee": code, "user": label,
                "status": "failed", "reason": exc.message,
            }
            continue

        stats[outcome] += 1
        yield {
            "type": "progress", "current": i, "total": total,
            "franchisee": code, "user": label,
            "status": outcome,
            "reason": "Already in the group" if outcome == "already_member" else "",
        }

    totals = {"added": 0, "already_member": 0, "skipped": 0, "failed": 0}
    for stats in per_franchisee.values():
        for key in totals:
            totals[key] += stats[key]

    yield {
        "type": "done",
        "summary": {
            "total": total,
            "per_franchisee": per_franchisee,
            "groups": resolved,
            "failures": failures,
            **totals,
        },
    }
