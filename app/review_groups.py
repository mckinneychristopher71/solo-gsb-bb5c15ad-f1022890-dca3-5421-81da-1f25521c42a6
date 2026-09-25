"""同案合并复核 (same-case merged review).

Pending (``needs_review``) match records are grouped by the *normalised* form
of their query: road (street) tokens, locality tokens and the house number.
Within such a normalisation bucket, only records whose candidate address IDs,
candidate order **and** candidate scores are all identical share a group --
the group key is a digest of both parts to make that explicit.

A group review (confirm best / reassign a shared candidate / mark no-match for
the whole group) is applied inside a single SQLite write transaction: the
group is recomputed from the live table after the write lock is acquired, and
every member is re-checked to still be pending with an unchanged signature and
member set.  Any drift aborts the transaction -- nothing is written -- and the
caller gets a conflict (HTTP 409).  The group review is likewise refused
wholesale while any member is still claimed by a reviewer (see
``app/review_claims.py``), so the merged path cannot bypass an active lease.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db
from .consensus import norm_key


class GroupReviewError(Exception):
    """Invalid group-review request (bad action / candidate not shared)."""


class GroupConflict(GroupReviewError):
    """The group changed since the client loaded it; nothing was written."""


def _candidate_signature(candidates: list[dict[str, Any]]) -> list[list[Any]]:
    """Ordered ``[address_id, score]`` pairs -- must match exactly within a group."""
    return [[c["address"]["id"], c["score"]] for c in candidates]


def _group_key(norm_key: dict[str, Any],
               signature: list[list[Any]]) -> str:
    payload = json.dumps(
        {"norm": norm_key, "candidates": signature},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Group all pending review records; each group carries its shared candidates.

    Members are annotated with their active review claim (if any) so the
    queue can show who holds which record and the group review can refuse to
    bypass an active occupancy.
    """
    rows = conn.execute(
        "SELECT * FROM match_logs WHERE status = 'needs_review' ORDER BY id"
    ).fetchall()
    claims = db.active_claim_map(conn, [row["id"] for row in rows])
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        candidates = json.loads(row["candidates_json"])
        nkey = norm_key(row["query"])
        key = _group_key(nkey, _candidate_signature(candidates))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "group_key": key,
                "street_tokens": nkey["street"],
                "locality_tokens": nkey["locality"],
                "house": nkey["house"] or None,
                "candidates": candidates,
                "queries": [],
            }
            buckets[key] = bucket
        claim = claims.get(row["id"])
        bucket["queries"].append({
            "id": row["id"],
            "query": row["query"],
            "created_at": row["created_at"],
            "claimed_by": claim["reviewer"] if claim else None,
            "claim_expires_at": claim["expires_at"] if claim else None,
        })
    groups: list[dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["count"] = len(bucket["queries"])
        bucket["match_ids"] = [m["id"] for m in bucket["queries"]]
        groups.append(bucket)
    # Largest groups first, then oldest record first for a stable display.
    groups.sort(key=lambda g: (-g["count"], g["match_ids"][0]))
    return groups


def apply_group_review(conn: sqlite3.Connection, *, group_key: str,
                       match_ids: list[int], action: str,
                       selected_id: int | None = None) -> dict[str, Any]:
    """Review a whole group atomically.

    The group is recomputed inside one ``BEGIN IMMEDIATE`` transaction: every
    submitted member must still be pending, and the group signature and member
    set must be exactly what the client saw.  Any mismatch raises
    :class:`GroupConflict` and rolls everything back; on success each member
    gets its own review decision and corresponding score.
    """
    requested = sorted({int(m) for m in match_ids})
    if not requested:
        raise GroupReviewError("成员集合不能为空")

    with db.immediate_transaction(conn):
        groups = compute_groups(conn)
        group = next((g for g in groups if g["group_key"] == group_key), None)
        if group is None:
            raise GroupConflict("同案组已不存在或成员已全部复核，请刷新后重试")
        if sorted(group["match_ids"]) != requested:
            raise GroupConflict("同案组成员集合已变化，请刷新后重试")

        # A member with an active claim is being handled individually: refuse
        # the whole group so the merged path cannot bypass that occupancy.
        claimed = [q for q in group["queries"] if q["claimed_by"]]
        if claimed:
            first = claimed[0]
            raise GroupConflict(
                f"组成员 #{first['id']} 正被 {first['claimed_by']} 领取"
                f"（租约至 {first['claim_expires_at']}），"
                "为避免绕过占用，整组未写入；请先释放或等待租约到期"
            )

        shared_ids = {c["address"]["id"] for c in group["candidates"]}
        if action == "overridden":
            if selected_id is None:
                raise GroupReviewError("改派时必须提供 selected_id")
            if selected_id not in shared_ids:
                raise GroupReviewError("所选地址不在该组的共同候选中")
            override_text = next(
                c["address"]["full_text"] for c in group["candidates"]
                if c["address"]["id"] == selected_id
            )

        for match_id in group["match_ids"]:
            row = db.get_match_log(conn, match_id)
            if row is None or row["status"] != "needs_review":
                raise GroupConflict("组内存在已复核记录，整组未写入")
            member_candidates = json.loads(row["candidates_json"])
            if action == "confirmed":
                member_selected, member_score = row["candidate_id"], row["score"]
                # Snapshot the chosen full text so the verdict survives a later
                # deletion of the address row.
                member_text = next(
                    (c["address"]["full_text"] for c in member_candidates
                     if c["address"]["id"] == row["candidate_id"]),
                    None,
                )
            elif action == "overridden":
                member_selected = selected_id
                member_text = override_text
                member_scores = {
                    c["address"]["id"]: c["score"]
                    for c in member_candidates
                }
                member_score = member_scores[selected_id]
            else:  # rejected
                member_selected, member_score = None, None
                member_text = None
            db.review_match_log(
                conn,
                match_id,
                action=action,
                selected_id=member_selected,
                score=member_score,
                selected_text=member_text,
            )

    return {
        "group_key": group_key,
        "action": action,
        "selected_id": selected_id if action == "overridden" else None,
        "reviewed": len(requested),
        "match_ids": requested,
    }
