"""人工复核领取 (manual-review claim leases).

A reviewer types their name to *claim* a pending (``needs_review``) record and
receives a bearer token valid for a short lease (10 minutes by default,
``REVIEW_LEASE_SECONDS``).  Claiming and releasing run inside a single SQLite
write transaction (``BEGIN IMMEDIATE``), so two concurrent claims cannot both
succeed: at most one token per record is valid at any moment, and once the
lease has expired the record can be claimed again (the old token stays in the
table as an audit trail but is permanently stale).

A single-record review submission must carry the token.  The review endpoint
verifies the lease **and** the still-pending status inside the same write
transaction that writes the verdict; expired tokens, stale (released /
superseded) tokens and duplicate submissions are all rejected with a conflict
(HTTP 409).  A successful review consumes the claim in the same transaction.

The same-case group review does not use tokens at all -- instead it refuses
the whole group (409, nothing written) while any member is still claimed, so
the merged path cannot bypass an active occupancy.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db


class ClaimError(Exception):
    """Base class for claim/lease failures."""


class ClaimNotFound(ClaimError):
    """Record or token does not exist (HTTP 404)."""


class ClaimConflict(ClaimError):
    """Lease state forbids the operation (HTTP 409); nothing was written."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def claim_record(conn: sqlite3.Connection, match_id: int, *, reviewer: str,
                 ttl_seconds: int) -> dict[str, Any]:
    """Claim one pending record for ``reviewer``; returns the new lease.

    The whole check-and-insert runs inside one ``BEGIN IMMEDIATE``
    transaction: the record must exist, still be ``needs_review`` and have no
    active (unreleased, unexpired) claim.  Any violation raises
    :class:`ClaimNotFound` / :class:`ClaimConflict` and rolls back.
    """
    with db.immediate_transaction(conn):
        row = db.get_match_log(conn, match_id)
        if row is None:
            raise ClaimNotFound("匹配记录不存在")
        if row["status"] != "needs_review":
            raise ClaimConflict(
                f"记录 #{match_id} 当前不是待确认状态，不能领取复核租约"
            )
        active = db.get_active_claim(conn, match_id)
        if active is not None:
            raise ClaimConflict(
                f"记录 #{match_id} 已被 {active['reviewer']} 领取，"
                f"租约至 {active['expires_at']}，到期后才能重新领取"
            )
        token = secrets.token_urlsafe(24)
        expires_at = db.insert_claim(
            conn, match_id=match_id, token=token,
            reviewer=reviewer, ttl_seconds=ttl_seconds,
        )
    return {
        "ok": True,
        "match_id": match_id,
        "token": token,
        "reviewer": reviewer,
        "lease_seconds": ttl_seconds,
        "expires_at": expires_at,
    }


def release_record(conn: sqlite3.Connection, match_id: int, *,
                   token: str) -> dict[str, Any]:
    """Release an active claim early; only the current valid token may do so.

    Runs inside one write transaction.  An unknown token is a 404; a token
    that is already released or has expired is a 409 (it is no longer the
    valid token for the record).
    """
    with db.immediate_transaction(conn):
        row = db.get_match_log(conn, match_id)
        if row is None:
            raise ClaimNotFound("匹配记录不存在")
        claim = db.get_claim_by_token(conn, match_id, token)
        if claim is None:
            raise ClaimNotFound("领取令牌不存在")
        if claim["released_at"] is not None:
            raise ClaimConflict("该领取令牌已释放（旧令牌），不能重复释放")
        if claim["expires_at"] <= _now():
            raise ClaimConflict("该领取令牌已过期，无需释放；记录可重新领取")
        db.release_claim(conn, claim["id"])
    return {
        "ok": True,
        "match_id": match_id,
        "released": True,
        "reviewer": claim["reviewer"],
    }


def require_active_claim(conn: sqlite3.Connection, match_id: int,
                         token: str | None) -> sqlite3.Row:
    """Return the valid claim row for ``token`` or raise :class:`ClaimConflict`.

    Must be called inside the review write transaction, after the record's
    pending status has been re-checked.  Because the claim transaction only
    ever inserts when no active claim exists, an unreleased + unexpired row
    is guaranteed to be *the* one valid token for the record.
    """
    if not token:
        raise ClaimConflict("单条复核须先领取记录，并携带领取令牌提交")
    claim = db.get_claim_by_token(conn, match_id, token)
    if claim is None:
        raise ClaimConflict("复核令牌无效：请先领取该记录再提交复核")
    if claim["released_at"] is not None:
        raise ClaimConflict("复核令牌已释放或已被使用（旧令牌），请重新领取")
    if claim["expires_at"] <= _now():
        raise ClaimConflict("复核令牌已过期，请重新领取后再提交")
    return claim


def consume_claim(conn: sqlite3.Connection, claim_id: int) -> None:
    """Mark the claim as used once its review verdict has been written."""
    db.release_claim(conn, claim_id)


def serialize_claim(claim: sqlite3.Row | None) -> dict[str, Any] | None:
    """Public view of an active claim (never exposes the token)."""
    if claim is None:
        return None
    return {
        "reviewer": claim["reviewer"],
        "claimed_at": claim["created_at"],
        "expires_at": claim["expires_at"],
    }
