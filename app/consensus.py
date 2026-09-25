"""复核共识记忆 (review consensus memory).

Every match is still scored exactly as before -- this module never changes a
score.  After scoring, the reviewed history is consulted: a *consensus* for
the query's normalised identity exists when

* at least **two** already-reviewed match records share the same normalised
  road tokens, locality tokens and house number;
* every one of them is a positive decision (``confirmed`` / ``overridden``)
  pointing at the **same** still-existing standard address -- there must be
  no rejection and no different conclusion for that key;
* that address is among the current match's candidates.

Only when all three hold, and only for a result that would otherwise wait in
the human review queue, is the verdict automatically redirected to the
consensus address.  If the target is not among the current candidates, or the
history is conflicting, the original verdict is kept and a reason is returned.

New reviews take effect on the very next match (the history is read live);
old match records are never rewritten.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .matcher import MatchResult
from .normalization import Normalized, normalize

# A consensus needs at least this many independent positive review records.
MIN_EVIDENCE = 2


def norm_key(query: str) -> dict[str, Any]:
    """Canonical identity of a query: normalised road/locality tokens + house."""
    return norm_key_for(normalize(query))


def norm_key_for(n: Normalized) -> dict[str, Any]:
    return {
        "street": sorted(n.street_tokens),
        "locality": sorted(n.locality_tokens),
        "house": n.house or "",
    }


def lookup_consensus(conn: sqlite3.Connection, query: str) -> dict[str, Any]:
    """Aggregate the reviewed history for ``query``'s normalised key.

    Returns a verdict-shaped dict whose ``state`` is one of:

    * ``hit``        - >=2 positive records, unanimous on one valid target;
    * ``conflict``   - a rejection or differing target exists;
    * ``target_gone``- unanimous but the target address no longer exists;
    * ``insufficient``- fewer than two positive records.

    Only reviewed (``status = 'reviewed'``) records count as evidence.
    """
    key = norm_key(query)
    rows = conn.execute(
        """
        SELECT id, query, review_action, selected_id, selected_address_id,
               selected_address_text, candidate_id, reviewed_at
          FROM match_logs
         WHERE status = 'reviewed' AND reviewed_at IS NOT NULL
         ORDER BY id
        """,
    ).fetchall()

    evidence: list[dict[str, Any]] = []
    rejected = 0
    stale = 0
    targets: set[int] = set()
    for row in rows:
        if norm_key(row["query"]) != key:
            continue
        if row["review_action"] == "rejected":
            rejected += 1
            continue
        # Prefer the durable review-time snapshot ID: unlike the FK column it
        # is not nulled when the address row is deleted, so a verdict pointing
        # at a now-deleted address can still be told apart from a legacy
        # verdict whose original target is genuinely unknown.
        target_id = row["selected_address_id"]
        if target_id is None:
            target_id = row["selected_id"]
        if target_id is None:
            # A positive decision with no determinable target (an address
            # deleted before review-time snapshots existed) is stale evidence,
            # not a rejection.
            stale += 1
            continue
        targets.add(target_id)
        evidence.append({
            "match_id": row["id"],
            "action": row["review_action"],
            "selected_id": target_id,
            "reviewed_at": row["reviewed_at"],
        })

    base = {
        "norm_key": key,
        "evidence_count": len(evidence),
        "rejected_count": rejected,
        "stale_count": stale,
        "required": MIN_EVIDENCE,
        "target_id": None,
        "target": None,
        "evidence": evidence,
    }

    if rejected:
        return {
            **base,
            "state": "conflict",
            "applied": False,
            "reason": f"历史复核中存在 {rejected} 条拒绝（无匹配）结论，不构成共识",
        }
    if len(targets) > 1:
        return {
            **base,
            "state": "conflict",
            "applied": False,
            "reason": "历史复核结论指向了不同的标准地址，结论冲突，保持原判",
        }

    # A unanimous, known target that no longer exists: the durable snapshot
    # keeps the ID even though the address row (and the FK) are gone.
    if targets:
        target_id = next(iter(targets))
        if db.get_address(conn, target_id) is None:
            return {
                **base,
                "state": "target_gone",
                "applied": False,
                "target_id": target_id,
                "reason": f"共识指向的标准地址 #{target_id} 已不存在，保持原判",
            }
    elif stale:
        # Only legacy records with a genuinely unknown original target remain.
        return {
            **base,
            "state": "target_gone",
            "applied": False,
            "reason": (
                f"历史复核确认的标准地址已被删除（{stale} 条结论失效），"
                "无法形成有效共识，保持原判"
            ),
        }

    if len(evidence) < MIN_EVIDENCE:
        return {
            **base,
            "state": "insufficient",
            "applied": False,
            "reason": (
                f"仅有 {len(evidence)} 条同向复核记录，"
                f"至少需要 {MIN_EVIDENCE} 条才形成共识"
            ),
        }

    target_id = next(iter(targets))
    target_row = db.get_address(conn, target_id)
    if target_row is None:
        return {
            **base,
            "state": "target_gone",
            "applied": False,
            "reason": f"共识指向的标准地址 #{target_id} 已不存在，保持原判",
        }

    return {
        **base,
        "state": "hit",
        "applied": False,
        "target_id": target_id,
        "target": {"id": target_row["id"], "full_text": target_row["full_text"]},
        "reason": (
            f"命中复核共识：{len(evidence)} 条已复核记录一致确认标准地址 "
            f"#{target_id}，自动指向该地址"
        ),
    }


def apply_consensus(result: MatchResult, consensus: dict[str, Any] | None) -> None:
    """Annotate ``result`` with the consensus verdict and, when eligible,
    redirect a would-be ``needs_review`` result to the consensus target.

    Scoring (status/reason/best) is left untouched unless a consensus hit is
    actually applied.  A consensus whose target is outside the current
    candidate set, or any non-hit state, preserves the original verdict.
    """
    if consensus is None:
        return
    candidate_ids = [c.address["id"] for c in result.candidates]
    target_id = consensus.get("target_id")

    if consensus["state"] == "hit":
        if target_id not in candidate_ids:
            consensus["state"] = "not_candidate"
            consensus["applied"] = False
            consensus["reason"] = (
                f"共识地址 #{target_id} 不在本次候选中，保持原判"
            )
        elif result.status != "needs_review":
            # Auto-matched / no-match verdicts are reported as-is; the
            # consensus is surfaced for transparency but does not redirect.
            consensus["state"] = "not_applied"
            consensus["applied"] = False
            consensus["reason"] = (
                f"存在复核共识（{consensus['evidence_count']} 条证据，"
                f"指向 #{target_id}），但本次评分原判为"
                f"{result.status}，维持原判"
            )
        else:
            consensus["applied"] = True
            original_status, original_reason = result.status, result.reason
            consensus["original_status"] = original_status
            consensus["original_reason"] = original_reason
            consensus["reason"] = (
                f"命中复核共识：{consensus['evidence_count']} 条已复核记录一致"
                f"确认标准地址 #{target_id}，自动指向该地址"
                f"（原判：{original_reason}）"
            )
            result.status = "auto_matched"
            result.reason = consensus["reason"]
            result.best = next(
                c for c in result.candidates if c.address["id"] == target_id
            )
            result.runner_up = None
    else:
        consensus["applied"] = False

    result.consensus = consensus
