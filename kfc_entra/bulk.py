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
from .report import EmployeeRow, is_community_admin_role


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
    grouping: str = "franchisee",
    promote_community_admins: bool = False,
    email_lookup: dict[str, str | None] | None = None,
    invite_missing: bool = False,
    invite_redirect_url: str = "https://myapps.microsoft.com",
    send_invitation_message: bool = True,
    email_allowlist: set[str] | None = None,
) -> Iterator[dict]:
    """Add (and optionally remove + delete) report rows from per-bucket groups.

    ``grouping`` decides which column gives the bucket key:
      * "franchisee" (default) - row.franchisee, normalised to UPPERCASE
      * "store"                - row.store, preserved verbatim
    ``promote_community_admins`` only fires in store mode: after each
    successful batch-add, any user whose row.job_role is in
    COMMUNITY_ADMIN_ROLES is also promoted to a group owner (the Viva
    Engage "community admin" role), batched 20 at a time via Graph $batch.

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
    bucket_label = "Store" if grouping == "store" else "Franchisee"
    yield {"type": "phase", "message": f"Resolving {bucket_label} groups..."}

    def _normalise_code(c: str) -> str:
        # Franchisee codes are upper-case ("ANR", "BWF"); store names are
        # left verbatim so "Salamander Bay" stays "Salamander Bay".
        return c.strip() if grouping == "store" else c.strip().upper()

    resolved: dict[str, dict] = {}
    for code, assignment in assignments.items():
        code = _normalise_code(code)
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
    def _row_code(row: EmployeeRow) -> str:
        if grouping == "store":
            return (row.store or "UNKNOWN").strip()
        return (row.franchisee or "UNK").strip().upper()

    def _row_is_corporate(row: EmployeeRow) -> bool:
        # Skip-by-default rows: blank / UNK / UNKNOWN buckets that don't
        # represent a real place users should be added to.
        if grouping == "store":
            return _row_code(row).upper() in {"", "UNK", "UNKNOWN"}
        return row.is_unknown

    # Normalise the retry allowlist (if any) to lowercase emails so it
    # matches the EmployeeRow.email field which the parser lowercases.
    allowed = (
        {(e or "").strip().lower() for e in email_allowlist if e}
        if email_allowlist is not None
        else None
    )

    work: list[EmployeeRow] = []
    untouched_by_code: dict[str, int] = {}
    for row in rows:
        code = _row_code(row)
        if _row_is_corporate(row) and not include_unknown:
            untouched_by_code[code] = untouched_by_code.get(code, 0) + 1
            continue
        if code not in resolved:
            untouched_by_code[code] = untouched_by_code.get(code, 0) + 1
            continue
        # Retry mode: only process rows whose email made the allowlist.
        if allowed is not None and (row.email or "").strip().lower() not in allowed:
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
        yield {"type": "phase", "message": "Cross-referencing emails against the tenant..."}
    pending: list[dict] = []
    skipped_no_user: list[dict] = []

    # Source of truth is the tenant, not the report: every email is looked
    # up by /users?$filter=mail eq <e> ... regardless of what the report's
    # Yammer ID column says. The Yammer ID column only flips to a UUID
    # once a guest activates Viva Engage, so pre-invited-but-not-yet-
    # activated users would be wrongly classified as "not in Entra" if we
    # trusted it. Email cache is preferred over a fresh lookup when the
    # caller (the web route) passes one in - it's the result of the
    # preview's "Check against tenant" pass and is good to re-use.
    email_cache: dict[str, str | None] = (
        dict(email_lookup) if email_lookup else {}
    )

    pending_emails: list[str] = []
    seen: set[str] = set()
    for row in work:
        e = (row.email or "").strip()
        if not e or e in seen:
            continue
        seen.add(e)
        # Only look up emails we don't already have a cached answer for.
        if e not in email_cache:
            pending_emails.append(e)

    BATCH_SIZE = 20
    if pending_emails:
        yield {
            "type": "phase",
            "message": (
                f"Looking up {len(pending_emails)} email(s) in Entra "
                "(batched 20 at a time)..."
            ),
        }
        for i in range(0, len(pending_emails), BATCH_SIZE):
            chunk = pending_emails[i : i + BATCH_SIZE]
            try:
                results = client.batch_get_users_by_email(chunk)
            except GraphError:
                results = {e: None for e in chunk}
            for email, user in results.items():
                email_cache[email] = user.id if user else None
            done = min(i + BATCH_SIZE, len(pending_emails))
            if done % 400 == 0 or done == len(pending_emails):
                yield {
                    "type": "phase",
                    "message": f"Resolved {done} of {len(pending_emails)} emails...",
                }

    # --- if invite_missing: stage a Guest invitation for every email the
    # lookup phase couldn't resolve. Actual invites fire AFTER the "start"
    # event so each one emits a per-row progress event (visible in the
    # bulk-panel log), and the total bar reflects them too.
    INVITE_PLACEHOLDER = "__pending_invite__"
    invite_candidates: dict[str, str] = {}
    if invite_missing:
        for row in work:
            e = (row.email or "").strip()
            if not e or email_cache.get(e):
                continue
            invite_candidates.setdefault(e, row.name or e.split("@", 1)[0])
        # Optimistic cache - classify these rows AS IF the invite already
        # succeeded so they land in `pending` rather than `skipped`. After
        # each batch invite finishes we patch the real user_id in.
        for email in invite_candidates:
            email_cache[email] = INVITE_PLACEHOLDER

    for row in work:
        code = _row_code(row)
        label = row.email or row.name or f"row {row.row_number}"
        target = resolved[code]  # guaranteed - we filtered to mapped-only
        email_key = (row.email or "").strip()

        # Always the tenant's answer; ignore the Yammer ID column entirely.
        user_id = email_cache.get(email_key) if email_key else None

        if not user_id:
            skipped_no_user.append({"code": code, "label": label, "email": email_key})
        else:
            pending.append({
                "code": code, "label": label, "email": email_key,
                "user_id": user_id, "group_id": target["group_id"],
                # Row context so a "failed" event can include enough info
                # to be re-downloaded as a self-contained mini-report
                # that uploads cleanly through parse_report.
                "name": row.name,
                "store": row.store,
                "store_id": row.store_id,
                "job_role": row.job_role,
                "franchisee_id": row.franchisee,
                # Promote later if this row holds a community-admin role and
                # the caller opted in. Job role is normalised so
                # "Assistant Manager" / "assistant manager" / "Assistant  Manager"
                # all map to the same canonical key.
                "is_admin": (
                    promote_community_admins
                    and grouping == "store"
                    and is_community_admin_role(row.job_role)
                ),
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
    total_promotes = sum(1 for p in pending if p.get("is_admin")) if promote_community_admins else 0
    total_invites = len(invite_candidates)
    # Adds already counts every row with a (real or placeholder) user_id,
    # so subtract the invites that will resolve to adds to avoid double-
    # counting; failed invites end up as skipped via the patch step below.
    total = total_invites + total_skipped + total_adds + total_promotes + total_removes + total_deletes
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
                "owner_added": 0, "already_owner": 0,
                "invited": 0,
            },
        )

    # --- emit "skipped" events for users we couldn't resolve to Entra IDs.
    # These are rows where the email was blank, or invite_missing was off
    # and the lookup found no user. Invite failures emit their own
    # "failed" progress lines in the invite phase below.
    for s in skipped_no_user:
        emitted += 1
        stats = bucket(s["code"])
        stats["skipped"] += 1
        yield {
            "type": "progress", "current": emitted, "total": total,
            "franchisee": s["code"], "user": s["label"],
            "status": "skipped",
            "reason": "Not in Entra yet (no match by email)",
        }

    # --- invite phase: batch /invitations $batch (20 per round-trip),
    # patch the resulting user_id into the matching pending entries.
    # Each invitation emits a per-row progress event so the user can see
    # them landing in the log; failed invites are converted to skips on
    # the spot (the pending entry is dropped so the add phase doesn't
    # try to use a None user_id).
    if invite_candidates:
        yield {
            "type": "phase",
            "message": (
                f"Inviting {len(invite_candidates)} Guest(s) "
                "(batched 20 at a time)..."
            ),
        }
        invite_items = list(invite_candidates.items())
        for i in range(0, len(invite_items), BATCH_SIZE):
            chunk = invite_items[i : i + BATCH_SIZE]
            invitations = [
                {
                    "email": em,
                    "display_name": disp,
                    "redirect_url": invite_redirect_url,
                    "send_invitation_message": send_invitation_message,
                }
                for em, disp in chunk
            ]
            try:
                results = client.batch_invite_guests(invitations)
            except GraphError as exc:
                results = {em: (None, exc.message) for em, _ in chunk}
            for email, (uid, err) in results.items():
                emitted += 1
                code_for_email = ""
                if uid:
                    email_cache[email] = uid
                    # Patch every pending entry that's still on the placeholder.
                    for item in pending:
                        if item.get("email") == email and item.get("user_id") == INVITE_PLACEHOLDER:
                            item["user_id"] = uid
                            code_for_email = code_for_email or item["code"]
                    bucket(code_for_email or "UNK")["invited"] = bucket(code_for_email or "UNK").get("invited", 0) + 1
                    yield {
                        "type": "progress", "current": emitted, "total": total,
                        "franchisee": code_for_email, "user": email,
                        "status": "invited",
                        "reason": "Guest invitation sent" if send_invitation_message
                                  else "Guest account created (no email)",
                    }
                else:
                    # Drop the placeholder entry / entries so the add phase
                    # doesn't try to use it. Promote it to a skip line.
                    drop = [p for p in pending if p.get("email") == email and p.get("user_id") == INVITE_PLACEHOLDER]
                    dropped_ctx: dict = {}
                    for d in drop:
                        pending.remove(d)
                        code_for_email = code_for_email or d["code"]
                        if not dropped_ctx:
                            dropped_ctx = d
                    bucket(code_for_email or "UNK")["failed"] = bucket(code_for_email or "UNK").get("failed", 0) + 1
                    failures.append({
                        "user": email, "franchisee": code_for_email,
                        "email": email,
                        "name": dropped_ctx.get("name") or invite_candidates.get(email, ""),
                        "store": dropped_ctx.get("store", ""),
                        "store_id": dropped_ctx.get("store_id", ""),
                        "job_role": dropped_ctx.get("job_role", ""),
                        "franchisee_id": dropped_ctx.get("franchisee_id", ""),
                        "reason": err or "invite failed", "action": "invite",
                    })
                    yield {
                        "type": "progress", "current": emitted, "total": total,
                        "franchisee": code_for_email, "user": email,
                        "status": "failed",
                        "reason": f"Invite failed: {err or 'unknown error'}",
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
                        "email": c.get("email", ""),
                        "name": c.get("name", ""),
                        "store": c.get("store", ""),
                        "store_id": c.get("store_id", ""),
                        "job_role": c.get("job_role", ""),
                        "franchisee_id": c.get("franchisee_id", ""),
                        "reason": reason, "action": "add",
                    })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "franchisee": c["code"], "user": c["label"],
                    "status": outcome,
                    "reason": "Already in the group" if outcome == "already_member" else reason,
                }

    # --- batch-promote owners (Viva Engage "community admins"): RGM / AM /
    # AM Trainee / RGM Trainee from store mode get added as group owners,
    # batched 20 at a time. Independent of add success: a freshly-added
    # user can also be flagged as owner in the same pass, and an
    # already_member user can still be promoted.
    if promote_community_admins and total_promotes:
        yield {
            "type": "phase",
            "message": f"Promoting {total_promotes} community admin(s) to group owner...",
        }
        admins_by_group: dict[str, list[dict]] = {}
        for item in pending:
            if item.get("is_admin"):
                admins_by_group.setdefault(item["group_id"], []).append(item)
        for group_id, items in admins_by_group.items():
            for i in range(0, len(items), BATCH_SIZE):
                chunk = items[i : i + BATCH_SIZE]
                user_ids = [c["user_id"] for c in chunk]
                try:
                    results = client.batch_add_owners_to_group(group_id, user_ids)
                except GraphError as exc:
                    results = {uid: ("failed", exc.message) for uid in user_ids}
                for c in chunk:
                    emitted += 1
                    outcome, reason = results.get(c["user_id"], ("failed", "No batch response for this user"))
                    # Map Graph's "added"/"already_owner" to owner-specific
                    # outcome labels BEFORE the stats bucket so the
                    # per-Franchisee table and summary tiles count
                    # promotions separately from member-adds.
                    if outcome == "added":
                        outcome = "owner_added"
                    stats = bucket(c["code"])
                    stats[outcome] = stats.get(outcome, 0) + 1
                    if outcome == "failed":
                        failures.append({
                            "user": c["label"], "franchisee": c["code"],
                            "email": c.get("email", ""),
                            "name": c.get("name", ""),
                            "store": c.get("store", ""),
                            "store_id": c.get("store_id", ""),
                            "job_role": c.get("job_role", ""),
                            "franchisee_id": c.get("franchisee_id", ""),
                            "reason": reason, "action": "promote",
                        })
                    yield {
                        "type": "progress", "current": emitted, "total": total,
                        "franchisee": c["code"], "user": c["label"],
                        "status": outcome,
                        "reason": (
                            "Already an owner / community admin"
                            if outcome == "already_owner"
                            else reason if outcome == "failed"
                            else "Promoted to group owner (community admin)"
                        ),
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
              "deleted": 0, "already_gone": 0,
              "owner_added": 0, "already_owner": 0}
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
    """Pull email addresses out of a CSV / TSV / XLS / XLSX file.

    Looks for a column whose header contains 'email', 'mail', or 'upn'
    (case-insensitive). If no such header exists, falls back to the first
    column. Returns a de-duplicated, case-preserved list.
    """
    import csv as _csv
    import io as _io
    import pathlib as _pl
    suffix = _pl.Path(filename or "").suffix.lower()
    head = content[:4]
    rows: list[list[str]] = []

    if suffix in (".xlsx", ".xlsm") or head == b"PK\x03\x04":
        from openpyxl import load_workbook
        wb = load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        if ws is not None:
            for r in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v) for v in r])
    elif suffix == ".xls" or head == b"\xd0\xcf\x11\xe0":
        import xlrd
        book = xlrd.open_workbook(file_contents=content, formatting_info=False)
        sheet = book.sheet_by_index(0)
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                v = sheet.cell(r, c).value
                if isinstance(v, float) and v.is_integer():
                    v = str(int(v))
                elif v is None:
                    v = ""
                else:
                    v = str(v)
                row.append(v)
            rows.append(row)
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


def iter_cross_reference(
    client: GraphClient,
    emails: Iterable[str],
    lookup_sink: dict[str, str | None],
) -> Iterator[dict]:
    """Batch-lookup ``emails`` against the tenant, streaming progress.

    Writes resolved {email: user_id|None} mappings into ``lookup_sink``
    (mutated in place). Used by the report preview's "Check against
    tenant" button to populate badges without trusting the report's
    Yammer ID column for who's actually in Entra.

    Emits SSE-shaped events::

        {"type": "start", "total": N}
        {"type": "progress", "current": M, "total": N,
         "in_tenant": X, "missing": Y}
        {"type": "done", "in_tenant": X, "missing": Y, "total": N}
    """
    unique = []
    seen = set()
    for raw in emails:
        e = (raw or "").strip()
        if not e or e in seen:
            continue
        seen.add(e)
        unique.append(e)

    total = len(unique)
    yield {"type": "start", "total": total}

    BATCH = 20
    in_tenant = 0
    missing = 0
    for i in range(0, total, BATCH):
        chunk = unique[i : i + BATCH]
        try:
            results = client.batch_get_users_by_email(chunk)
        except GraphError:
            results = {e: None for e in chunk}
        for email, user in results.items():
            uid = user.id if user else None
            lookup_sink[email] = uid
            if uid:
                in_tenant += 1
            else:
                missing += 1
        yield {
            "type": "progress",
            "current": min(i + BATCH, total),
            "total": total,
            "in_tenant": in_tenant,
            "missing": missing,
        }
    yield {
        "type": "done",
        "in_tenant": in_tenant,
        "missing": missing,
        "total": total,
    }


def iter_invite_missing(
    client: GraphClient,
    candidates: list[tuple[str, str]],
    invite_redirect_url: str = "https://myapps.microsoft.com",
    send_invitation_message: bool = True,
) -> Iterator[dict]:
    """Invite a batch of report emails as Guests without touching groups.

    ``candidates`` is a list of ``(email, display_name)`` pairs - typically
    the rows whose email didn't resolve during the preview's cross-
    reference pass. We /invitations $batch them 20 at a time, emit one
    progress event per result, and finish with a summary the bulk panel
    can render.

    Used by the "Invite missing users" button on the report preview, so
    you can populate the tenant with the report's Guests before going
    near the apply / mapping flow.
    """
    total = len(candidates)
    yield {"type": "start", "total": total}
    if total == 0:
        yield {
            "type": "done",
            "summary": {
                "total": 0, "succeeded": 0, "invited": 0,
                "failed": 0, "failures": [],
            },
        }
        return

    BATCH_SIZE = 20
    invited = 0
    failures: list[dict] = []
    emitted = 0
    invited_uids: dict[str, str] = {}

    for i in range(0, total, BATCH_SIZE):
        chunk = candidates[i : i + BATCH_SIZE]
        invitations = [
            {
                "email": em,
                "display_name": dn or em.split("@", 1)[0],
                "redirect_url": invite_redirect_url,
                "send_invitation_message": send_invitation_message,
            }
            for em, dn in chunk
        ]
        try:
            results = client.batch_invite_guests(invitations)
        except GraphError as exc:
            results = {em: (None, exc.message) for em, _ in chunk}
        for email, (uid, err) in results.items():
            emitted += 1
            if uid:
                invited += 1
                invited_uids[email] = uid
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "user": email,
                    "status": "invited",
                    "reason": (
                        "Guest invitation sent" if send_invitation_message
                        else "Guest account created (no email)"
                    ),
                }
            else:
                failures.append({
                    "user": email,
                    "email": email,
                    "reason": err or "invite failed",
                    "action": "invite",
                })
                yield {
                    "type": "progress", "current": emitted, "total": total,
                    "user": email,
                    "status": "failed",
                    "reason": f"Invite failed: {err or 'unknown error'}",
                }

    yield {
        "type": "done",
        "summary": {
            "total": total,
            "succeeded": invited,
            "invited": invited,
            "failed": len(failures),
            "failures": failures,
        },
    }


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
    # --- resolve each email (batched 20 at a time via Graph $batch) ---------
    email_list = [e for e in emails if e]
    yield {
        "type": "phase",
        "message": f"Resolving {len(email_list)} email(s) to Entra accounts (batched 20 at a time)...",
    }
    resolved: list[dict] = []
    not_found: list[str] = []
    BATCH = 20
    for i in range(0, len(email_list), BATCH):
        chunk = email_list[i : i + BATCH]
        try:
            lookups = client.batch_get_users_by_email(chunk)
        except GraphError:
            lookups = {e: None for e in chunk}
        for email in chunk:
            user = lookups.get(email)
            if user is None:
                not_found.append(email)
            else:
                resolved.append({
                    "email": email, "user_id": user.id,
                    "display_name": user.display_name,
                })
        done = min(i + BATCH, len(email_list))
        if done % 400 == 0 or done == len(email_list):
            yield {
                "type": "phase",
                "message": f"Resolved {done} of {len(email_list)} emails...",
            }

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
