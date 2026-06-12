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
    remove_missing: bool = False,
    delete_missing: bool = False,
) -> Iterator[dict]:
    """Add (and optionally remove + delete) report rows from Franchisee groups.

    ``assignments`` maps Franchisee code -> {"group_id": str|None,
    "group_name": str}. Codes mapped to a name without an id get the group
    created first; the new mapping is persisted immediately.

    When ``remove_missing`` is True, each mapped group is also reconciled:
    users currently in the group who don't appear in the report for that
    Franchisee get removed (DELETE /groups/{id}/members/{uid}/$ref via the
    Graph $batch endpoint, 20 at a time).
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

    # --- resolve user IDs for every row before emitting any progress so we
    # can compute the true total (adds + removes + unresolved-skips) up
    # front and drive the progress bar in one continuous pass.
    if work:
        yield {"type": "phase", "message": "Resolving user accounts..."}
    pending: list[dict] = []
    skipped_no_user: list[dict] = []
    email_cache: dict[str, str | None] = {}
    for row in work:
        code = (row.franchisee or "UNK").strip().upper()
        label = row.email or row.name or f"row {row.row_number}"
        target = resolved[code]  # guaranteed - we filtered to mapped-only

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
            skipped_no_user.append({"code": code, "label": label})
        else:
            pending.append({
                "code": code, "label": label,
                "user_id": user_id, "group_id": target["group_id"],
            })

    total_skipped = len(skipped_no_user)
    total_adds = len(pending)

    # --- if remove_missing: for each resolved group, list current members
    # and compute who's there but not in the report. Done BEFORE yielding
    # "start" so the total reflects the whole operation - the progress bar
    # fills smoothly across both add and remove phases.
    removals_by_group: dict[str, list[dict]] = {}
    if remove_missing:
        yield {"type": "phase", "message": "Finding users no longer in the report..."}
        report_uids_by_group: dict[str, set[str]] = {}
        for item in pending:
            report_uids_by_group.setdefault(item["group_id"], set()).add(item["user_id"])
        for code, info in resolved.items():
            gid = info["group_id"]
            try:
                current_members = client.list_group_members(gid)
            except GraphError as exc:
                yield {
                    "type": "phase",
                    "message": (
                        f"Couldn't list members of {info.get('group_name') or code}: "
                        f"{exc.message}. Skipping its removal pass."
                    ),
                }
                continue
            keep = report_uids_by_group.get(gid, set())
            to_remove = [m for m in current_members if m.id not in keep]
            if to_remove:
                removals_by_group[gid] = [
                    {
                        "code": code,
                        "user_id": m.id,
                        "label": m.mail or m.user_principal_name or m.display_name or m.id,
                    }
                    for m in to_remove
                ]

    total_removes = sum(len(v) for v in removals_by_group.values())

    # --- if delete_missing: each user who's about to be removed will ALSO be
    # deleted from the tenant (DELETE /users/{id}). Compute unique user ids
    # so a user in three mapped groups counts as a single delete - not three.
    unique_users_to_delete: list[dict] = []
    if delete_missing and remove_missing:
        seen_uids: set[str] = set()
        for items in removals_by_group.values():
            for item in items:
                if item["user_id"] in seen_uids:
                    continue
                seen_uids.add(item["user_id"])
                unique_users_to_delete.append({
                    "user_id": item["user_id"],
                    "code": item["code"],
                    "label": item["label"],
                })
    total_deletes = len(unique_users_to_delete)

    total = total_skipped + total_adds + total_removes + total_deletes
    yield {"type": "start", "total": total}

    per_franchisee: dict[str, dict] = {}
    failures: list[dict] = []
    emitted = 0

    def bucket(code: str) -> dict:
        return per_franchisee.setdefault(
            code,
            {
                "added": 0, "already_member": 0, "skipped": 0, "failed": 0,
                "removed": 0, "not_in_group": 0,
                "deleted": 0, "already_gone": 0,
            },
        )

    # --- emit "skipped" events for users we couldn't resolve to Entra IDs.
    for s in skipped_no_user:
        emitted += 1
        stats = bucket(s["code"])
        stats["skipped"] += 1
        yield {
            "type": "progress", "current": emitted, "total": total,
            "franchisee": s["code"], "user": s["label"],
            "status": "skipped",
            "reason": "Not in Entra yet (no Yammer ID and no match by email)",
        }

    # --- batch-add: 20 per Graph round-trip via $batch.
    by_group_add: dict[str, list[dict]] = {}
    for item in pending:
        by_group_add.setdefault(item["group_id"], []).append(item)

    BATCH_SIZE = 20
    for group_id, items in by_group_add.items():
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i : i + BATCH_SIZE]
            user_ids = [c["user_id"] for c in chunk]
            try:
                results = client.batch_add_members_to_group(group_id, user_ids)
            except GraphError as exc:
                results = {uid: ("failed", exc.message) for uid in user_ids}
            for c in chunk:
                emitted += 1
                outcome, reason = results.get(c["user_id"], ("failed", "No batch response for this user"))
                stats = bucket(c["code"])
                stats[outcome] = stats.get(outcome, 0) + 1
                if outcome == "failed":
                    failures.append({
                        "user": c["label"], "franchisee": c["code"],
                        "reason": reason, "action": "add",
                    })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "franchisee": c["code"], "user": c["label"],
                    "status": outcome,
                    "reason": "Already in the group" if outcome == "already_member" else reason,
                }

    # --- batch-remove: 20 per Graph round-trip via $batch (DELETE/$ref).
    if removals_by_group:
        yield {
            "type": "phase",
            "message": f"Removing {total_removes} user(s) no longer in the report...",
        }
    for group_id, items in removals_by_group.items():
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i : i + BATCH_SIZE]
            user_ids = [c["user_id"] for c in chunk]
            try:
                results = client.batch_remove_members_from_group(group_id, user_ids)
            except GraphError as exc:
                results = {uid: ("failed", exc.message) for uid in user_ids}
            for c in chunk:
                emitted += 1
                outcome, reason = results.get(c["user_id"], ("failed", "No batch response for this user"))
                stats = bucket(c["code"])
                stats[outcome] = stats.get(outcome, 0) + 1
                if outcome == "failed":
                    failures.append({
                        "user": c["label"], "franchisee": c["code"],
                        "reason": reason, "action": "remove",
                    })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "franchisee": c["code"], "user": c["label"],
                    "status": outcome,
                    "reason": (
                        reason if outcome == "failed"
                        else "Wasn't in the group" if outcome == "not_in_group"
                        else "Removed from the group"
                    ),
                }

    # --- batch-delete: when delete_missing is also on, every user whose
    # group membership was just revoked is moved to the tenant's Deleted
    # Users container. Recoverable for 30 days from the Entra portal but
    # invisible to normal queries until restored. 20 deletes per Graph
    # round-trip via $batch.
    if unique_users_to_delete:
        yield {
            "type": "phase",
            "message": (
                f"Deleting {total_deletes} user(s) from the tenant "
                f"(recoverable for 30 days from Entra > Deleted users)..."
            ),
        }
    for i in range(0, len(unique_users_to_delete), BATCH_SIZE):
        chunk = unique_users_to_delete[i : i + BATCH_SIZE]
        user_ids = [c["user_id"] for c in chunk]
        try:
            results = client.batch_delete_users(user_ids)
        except GraphError as exc:
            results = {uid: ("failed", exc.message) for uid in user_ids}
        for c in chunk:
            emitted += 1
            outcome, reason = results.get(c["user_id"], ("failed", "No batch response for this user"))
            stats = bucket(c["code"])
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome == "failed":
                failures.append({
                    "user": c["label"], "franchisee": c["code"],
                    "reason": reason, "action": "delete",
                })
            yield {
                "type": "progress", "current": emitted, "total": total,
                "franchisee": c["code"], "user": c["label"],
                "status": outcome,
                "reason": (
                    reason if outcome == "failed"
                    else "Already gone from the tenant" if outcome == "already_gone"
                    else "Deleted from tenant (recoverable for 30 days)"
                ),
            }

    totals = {"added": 0, "already_member": 0, "skipped": 0, "failed": 0,
              "removed": 0, "not_in_group": 0,
              "deleted": 0, "already_gone": 0}
    for stats in per_franchisee.values():
        for key in totals:
            totals[key] += stats.get(key, 0)

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


# ============================================================================
# Bulk offboard: remove every group membership for each email in an uploaded
# CSV / TSV / XLSX, and (optionally) disable each Entra account in the same
# pass. Idempotent and resumable - re-uploading a partially-processed CSV
# just sees "not_in_group" for the groups we already cleared and skips them.
# ============================================================================

def parse_offboard_emails(content: bytes, filename: str) -> list[str]:
    """Pull email addresses out of a CSV / TSV / XLSX file.

    Looks for a column whose header contains 'email', 'mail', or 'upn'
    (case-insensitive). If no such header exists, falls back to the first
    column. Returns a de-duplicated, case-preserved list.
    """
    import csv as _csv
    import io as _io
    import pathlib as _pl
    suffix = _pl.Path(filename or "").suffix.lower()
    rows: list[list[str]] = []

    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        if ws is not None:
            for r in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v) for v in r])
    else:
        text = content.decode("utf-8-sig", errors="replace")
        try:
            dialect = _csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
        except _csv.Error:
            dialect = _csv.excel
        rows = [list(r) for r in _csv.reader(_io.StringIO(text), dialect=dialect)]

    if not rows:
        return []

    # Find email column. Header heuristics, then fall back to first column.
    header = [str(h or "").strip().lower() for h in rows[0]]
    email_idx = next(
        (i for i, h in enumerate(header)
         if "email" in h or "e-mail" in h or "mail" in h or "upn" in h),
        None,
    )
    if email_idx is None:
        email_idx, start = 0, 0
    else:
        start = 1

    seen: set[str] = set()
    out: list[str] = []
    for row in rows[start:]:
        if not row:
            continue
        val = row[email_idx] if email_idx < len(row) else ""
        s = (val or "").strip()
        if "@" not in s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def iter_bulk_offboard(
    client: GraphClient,
    emails: Iterable[str],
    disable_accounts: bool = False,
) -> Iterator[dict]:
    """Remove each user from every group they're in (and optionally disable).

    Streams progress events in the same vocabulary as the other bulk ops so
    the existing kfcRunBulk JS handles it without changes:
      phase    -> "Resolving N email(s)...", etc.
      start    -> {"total": N}
      progress -> per (email, group) or per (email, disable) item
      done     -> {"summary": {...}}
    """
    yield {"type": "phase", "message": "Resolving emails to Entra accounts..."}

    # --- resolve each email --------------------------------------------------
    resolved: list[dict] = []
    not_found: list[str] = []
    for email in emails:
        try:
            user = client.get_user_by_email(email)
        except GraphError:
            user = None
        if user is None:
            not_found.append(email)
        else:
            resolved.append({"email": email, "user_id": user.id,
                             "display_name": user.display_name})

    yield {
        "type": "phase",
        "message": (
            f"Found {len(resolved)} account(s); "
            f"{len(not_found)} email(s) had no matching Entra account."
        ),
    }

    # --- list each resolved user's groups ------------------------------------
    yield {"type": "phase", "message": "Listing group memberships..."}
    per_user_groups: dict[str, list[str]] = {}
    for item in resolved:
        try:
            groups = client.list_user_groups(item["user_id"])
            per_user_groups[item["user_id"]] = [g.id for g in groups]
        except GraphError:
            per_user_groups[item["user_id"]] = []

    # Pivot: group_id -> [{user_id, email}, ...] so we can batch-DELETE 20
    # users from the same group in one Graph $batch call.
    removals_by_group: dict[str, list[dict]] = {}
    for item in resolved:
        for gid in per_user_groups.get(item["user_id"], []):
            removals_by_group.setdefault(gid, []).append(
                {"user_id": item["user_id"], "email": item["email"]}
            )

    total_removals = sum(len(v) for v in removals_by_group.values())
    total_disables = len(resolved) if disable_accounts else 0
    total = len(not_found) + total_removals + total_disables
    yield {"type": "start", "total": total}

    failures: list[dict] = []
    emitted = 0

    # --- "not found" events --------------------------------------------------
    for email in not_found:
        emitted += 1
        yield {
            "type": "progress", "current": emitted, "total": total,
            "user": email, "status": "skipped",
            "reason": "No Entra account found for this email",
        }

    # --- batch-remove from each group ----------------------------------------
    BATCH_SIZE = 20
    for group_id, items in removals_by_group.items():
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i : i + BATCH_SIZE]
            uids = [c["user_id"] for c in chunk]
            try:
                results = client.batch_remove_members_from_group(group_id, uids)
            except GraphError as exc:
                results = {uid: ("failed", exc.message) for uid in uids}
            for c in chunk:
                emitted += 1
                outcome, reason = results.get(c["user_id"], ("failed", "No batch response"))
                if outcome == "failed":
                    failures.append({
                        "user": c["email"], "reason": reason, "action": "remove",
                    })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "user": c["email"], "status": outcome,
                    "reason": (
                        reason if outcome == "failed"
                        else "Wasn't in the group" if outcome == "not_in_group"
                        else "Removed from the group"
                    ),
                }

    # --- account disables ----------------------------------------------------
    if disable_accounts and resolved:
        yield {
            "type": "phase",
            "message": f"Disabling {len(resolved)} account(s)...",
        }
        for item in resolved:
            emitted += 1
            try:
                client.disable_user(item["user_id"])
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "user": item["email"], "status": "disabled",
                    "reason": "Account disabled",
                }
            except GraphError as exc:
                failures.append({
                    "user": item["email"], "reason": exc.message, "action": "disable",
                })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "user": item["email"], "status": "failed",
                    "reason": "Couldn't disable account: " + exc.message,
                }

    yield {
        "type": "done",
        "summary": {
            "total": total,
            "users_offboarded": len(resolved),
            "users_not_found": len(not_found),
            "groups_removed": total_removals - len([
                f for f in failures if f.get("action") == "remove"
            ]),
            "accounts_disabled": (
                total_disables - len([f for f in failures if f.get("action") == "disable"])
                if disable_accounts else 0
            ),
            "failures": failures,
        },
    }
