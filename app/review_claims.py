"""人工复核领取（reviewer claim / lease）.

A pending (``needs_review``) match record can be **claimed** by a reviewer:
they supply their name and receive an opaque token valid for ten minutes (the
TTL is configurable via ``REVIEW_LEASE_SECONDS``).  Only one live token exists
per record -- ``review_claims.match_id`` is the primary key and every claim /
release / review runs inside a ``BEGIN IMMEDIATE`` write transaction, so two
reviewers can never hold the record at the same time.  Once the lease expires
the record can be claimed again (the stale row is overwritten).

A single-record review must carry the token; inside the same write transaction
the lease is checked (exists, unexpired, same token) and the record is
re-checked to still be pending before the verdict is written.  Expired tokens,
old tokens (a newer lease exists) and duplicate submissions are all rejected
with HTTP 409.

A same-case **group** review must not be able to bypass the occupancy: if any
member currently has a live lease the whole group review is refused (409) and
nothing is written.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db

REVIEWER_MAX_LEN = 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClaimError(Exception):
    """Base class for claim/lease failures."""


class ClaimBadRequest(ClaimError):
    """Invalid request payload (empty name / missing token)."""


class ClaimNotFound(ClaimError):
    """The match record (or selected address) does not exist."""


class ClaimConflict(ClaimError):
    """Lease/state conflict -- mapped to HTTP 409; nothing was written."""


def _claim_payload(row: sqlite3.Row, ttl_seconds: int) -> dict[str, Any]:
    return {
        "match_id": int(row["match_id"]),
        "token": row["token"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "expires_at": row["expires_at"],
        "lease_seconds": ttl_seconds,
    }


def claim_lease(conn: sqlite3.Connection, *, match_id: int, reviewer: str,
                ttl_seconds: int) -> dict[str, Any]:
    """Claim a pending record for ``ttl_seconds`` and return the token.

    Fails with :class:`ClaimConflict` when the record is not pending or
    another reviewer holds a live lease.
    """
    name = (reviewer or "").strip()
    if not name:
        raise ClaimBadRequest("领取时必须输入复核员姓名")
    if len(name) > REVIEWER_MAX_LEN:
        raise ClaimBadRequest(f"复核员姓名不能超过 {REVIEWER_MAX_LEN} 个字符")

    with db.immediate_transaction(conn):
        row = db.get_match_log(conn, match_id)
        if row is None:
            raise ClaimNotFound("匹配记录不存在")
        if row["status"] != "needs_review":
            raise ClaimConflict("该记录已不在待确认状态，请刷新后重试")
        existing = db.get_review_claim(conn, match_id)
        if existing is not None and existing["expires_at"] > _now():
            raise ClaimConflict(
                f"该记录已由「{existing['claimed_by']}」领取，"
                "租约到期后才能重新领取"
            )
        token = secrets.token_urlsafe(24)
        db.upsert_review_claim(
            conn, match_id=match_id, token=token,
            claimed_by=name, ttl_seconds=ttl_seconds,
        )
        return _claim_payload(db.get_review_claim(conn, match_id), ttl_seconds)


def release_lease(conn: sqlite3.Connection, *, match_id: int,
                  token: str) -> dict[str, Any]:
    """Release a lease.  Only the holder's current token can release it.

    Releasing an already-gone lease is idempotent (``released: False``);
    offering a token that disagrees with the live lease is a 409 conflict so
    one reviewer cannot drop another reviewer's occupancy.
    """
    if not token:
        raise ClaimBadRequest("释放必须携带令牌")

    with db.immediate_transaction(conn):
        row = db.get_match_log(conn, match_id)
        if row is None:
            raise ClaimNotFound("匹配记录不存在")
        claim = db.get_review_claim(conn, match_id)
        if claim is None:
            return {"ok": True, "match_id": match_id, "released": False}
        live = claim["expires_at"] > _now()
        if live and not secrets.compare_digest(claim["token"], token):
            raise ClaimConflict(
                f"该记录当前由「{claim['claimed_by']}」占用，令牌不一致，不能释放"
            )
        if not live and not secrets.compare_digest(claim["token"], token):
            # An expired lease with a foreign/old token: there is nothing live
            # to release; report idempotently without touching the stale row.
            return {"ok": True, "match_id": match_id, "released": False}
        db.delete_review_claim(conn, match_id)
        return {"ok": True, "match_id": match_id, "released": True}


def _resolve_decision(conn: sqlite3.Connection, row: sqlite3.Row,
                      action: str,
                      selected_id: int | None) -> tuple[int | None, float | None, str | None]:
    """Compute (selected_id, score, selected_text) for a validated review."""
    if action == "overridden":
        if selected_id is None:
            raise ClaimBadRequest("改派时必须提供 selected_id")
        target = db.get_address(conn, selected_id)
        if target is None:
            raise ClaimNotFound("所选标准地址不存在")
        score = None
        for cand in json.loads(row["candidates_json"]):
            if cand["address"]["id"] == selected_id:
                score = cand["score"]
                break
        return selected_id, score, target["full_text"]

    if action == "confirmed":
        score = row["score"]
        selected_text = None
        if row["candidate_id"] is not None:
            chosen = db.get_address(conn, row["candidate_id"])
            if chosen is not None:
                selected_text = chosen["full_text"]
            else:
                # Defensive: candidate row gone before review; restore the
                # text from the log's own candidate snapshot.
                for cand in json.loads(row["candidates_json"]):
                    if cand["address"]["id"] == row["candidate_id"]:
                        selected_text = cand["address"]["full_text"]
                        break
        return row["candidate_id"], score, selected_text

    # rejected
    return None, None, None


def review_with_lease(conn: sqlite3.Connection, *, match_id: int, action: str,
                      selected_id: int | None, token: str | None) -> dict[str, Any]:
    """Write a single-record verdict guarded by the lease.

    All checks happen inside one ``BEGIN IMMEDIATE`` transaction: the record
    must still be ``needs_review`` and the presented token must identify the
    one live, unexpired lease.  Any failure raises :class:`ClaimConflict`
    (HTTP 409) and rolls the whole transaction back; on success the claim row
    is deleted in the same transaction.
    """
    if not token:
        raise ClaimConflict("单条复核必须先领取记录并携带有效令牌")

    with db.immediate_transaction(conn):
        row = db.get_match_log(conn, match_id)
        if row is None:
            raise ClaimNotFound("匹配记录不存在")
        if row["status"] != "needs_review":
            raise ClaimConflict("该记录已复核或不再是待确认状态，请勿重复提交")

        claim = db.get_review_claim(conn, match_id)
        if claim is None:
            raise ClaimConflict("该记录尚未被领取，请先领取后再提交复核")
        if claim["expires_at"] <= _now():
            raise ClaimConflict("领取租约已过期，请重新领取后再提交复核")
        if not secrets.compare_digest(claim["token"], token):
            raise ClaimConflict(
                "令牌与当前有效租约不一致（租约可能已被他人重新领取），请刷新后重试"
            )

        resolved_id, score, selected_text = _resolve_decision(
            conn, row, action, selected_id
        )
        db.review_match_log(
            conn,
            match_id,
            action=action,
            selected_id=resolved_id,
            score=score,
            selected_text=selected_text,
        )
        # The occupancy ends with the verdict; the row must not outlive it.
        db.delete_review_claim(conn, match_id)

    return {
        "ok": True,
        "match_id": match_id,
        "action": action,
        "selected_id": resolved_id,
        "claimed_by": claim["claimed_by"],
    }


def assert_group_members_free(conn: sqlite3.Connection,
                              match_ids: list[int]) -> None:
    """Reject a group review while any member has a live lease.

    This closes the bypass where a whole-group verdict would otherwise
    overwrite a record a reviewer is currently working on individually.
    Expired leases do not block (the records are free to take again).
    """
    active = db.active_review_claims(conn, list(match_ids))
    if active:
        detail = "；".join(
            f"#{mid} 由「{row['claimed_by']}」领取至 {row['expires_at']}"
            for mid, row in sorted(active.items())
        )
        raise ClaimConflict(
            "同案组内存在仍被领取（租约未过期）的成员，整组复核已整体拒绝：" + detail
        )
