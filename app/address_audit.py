"""标准地址自匹配巡检 (standard-address self-match audit).

Every standard address in the current table is re-scored against the **whole**
table with the **default** rules, using its own full text as the query. A
healthy table "recognises itself": each record should auto-match its own ID.
Records that fail to do so are listed as risks, in exactly one of three
categories:

* ``self_score_low``   自身分数不足 -- the record's own score is below the
  default auto-match threshold, so its full text can never auto-hit itself
  (whatever the competition looks like);
* ``best_drifted``     首选偏移 -- the own score reaches the threshold, but
  another address ranks first;
* ``margin_ambiguous`` 分差歧义 -- the record itself ranks first, yet its
  lead over the runner-up is smaller than the default margin threshold.

Each risk carries its **own score** plus every *other* address scoring at
least ``own_score - margin_threshold`` (the blocking candidates). That list
comes from the full scored table, **not** the usual top-5 candidate
truncation -- with several near-identical rows the record itself may not
even appear inside its own top 5.

The audit is read-only: it only runs the scorer against the live table. It
never writes match logs and never consults the review-consensus memory, so
uploading a new table and reopening the audit simply recomputes everything.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import Settings
from .matcher import CandidateResult, MatchResult, candidate_to_dict, match_address

CATEGORY_SELF_SCORE_LOW = "self_score_low"
CATEGORY_BEST_DRIFTED = "best_drifted"
CATEGORY_MARGIN_AMBIGUOUS = "margin_ambiguous"

VALID_CATEGORIES = (
    CATEGORY_SELF_SCORE_LOW,
    CATEGORY_BEST_DRIFTED,
    CATEGORY_MARGIN_AMBIGUOUS,
)

CATEGORY_LABELS = {
    CATEGORY_SELF_SCORE_LOW: "自身分数不足",
    CATEGORY_BEST_DRIFTED: "首选偏移",
    CATEGORY_MARGIN_AMBIGUOUS: "分差歧义",
}

# Display order: a wrong winner is the most urgent risk, a merely ambiguous
# lead the least; within a category the weakest self-scores come first.
_CATEGORY_RANK = {
    CATEGORY_BEST_DRIFTED: 0,
    CATEGORY_SELF_SCORE_LOW: 1,
    CATEGORY_MARGIN_AMBIGUOUS: 2,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _classify(own: CandidateResult, result: MatchResult,
              settings: Settings) -> tuple[str, str]:
    """Category + human-readable reason for one record that missed itself."""
    auto = settings.auto_threshold
    margin = settings.margin_threshold
    best = result.best
    if own.score < auto:
        return CATEGORY_SELF_SCORE_LOW, (
            f"自身得分 {_pct(own.score)} 低于自动命中阈值 {_pct(auto)}，"
            "以该地址全文查询无法自动命中自身"
        )
    if best is None or best.address["id"] != own.address["id"]:
        best_txt = (
            f"#{best.address['id']}（{_pct(best.score)}）" if best else "无候选"
        )
        return CATEGORY_BEST_DRIFTED, (
            f"自身得分 {_pct(own.score)} 已达自动命中阈值 {_pct(auto)}，"
            f"但首选为 {best_txt}，自身未排在首位"
        )
    # Self is first and reaches the threshold, yet the verdict was not
    # auto_matched: the lead over the runner-up must be too small.
    runner = result.runner_up
    gap = own.score - (runner.score if runner else 0.0)
    runner_txt = f"#{runner.address['id']}" if runner else "无次高分候选"
    return CATEGORY_MARGIN_AMBIGUOUS, (
        f"自身为首选（{_pct(own.score)}），但与次高分 {runner_txt} 的分差 "
        f"{_pct(gap)} 小于默认分差阈值 {_pct(margin)}，无法自动确认"
    )


def compute_audit(conn: sqlite3.Connection, settings: Settings,
                  category: str | None = None) -> dict[str, Any]:
    """Re-run every standard address against the live table and list risks.

    ``category`` optionally restricts the returned items to one category;
    the summary counts always cover every risk. The whole manifest is
    recomputed on each call, so a freshly uploaded table is reflected the
    next time the audit is opened. Nothing is persisted.
    """
    rows = db.all_addresses(conn)
    items: list[dict[str, Any]] = []
    for row in rows:
        # Full scored list (no top-5 truncation): the record itself and every
        # close competitor must be visible, however many rows tie in front.
        result = match_address(row["full_text"], rows, settings, top_n=None)
        best = result.best
        if (result.status == "auto_matched" and best is not None
                and best.address["id"] == row["id"]):
            continue  # healthy: auto-hits its own ID
        # The record is always part of the scored table, so this never misses.
        own = next(c for c in result.candidates if c.address["id"] == row["id"])
        floor = round(own.score - settings.margin_threshold, 4)
        blockers = [
            c for c in result.candidates
            if c.address["id"] != row["id"] and c.score >= floor
        ]
        cat, reason = _classify(own, result, settings)
        items.append({
            "address": {
                "id": row["id"],
                "full_text": row["full_text"],
                "house": row["house"],
                "external_id": row["external_id"],
            },
            "category": cat,
            "category_label": CATEGORY_LABELS[cat],
            "audit_reason": reason,
            "own_score": own.score,
            "blocker_floor": floor,
            "match_status": result.status,
            "match_reason": result.reason,
            "best_id": best.address["id"] if best else None,
            "best_score": best.score if best else None,
            "best_full_text": best.address["full_text"] if best else None,
            "blockers": [candidate_to_dict(c) for c in blockers],
        })

    items.sort(key=lambda it: (
        _CATEGORY_RANK[it["category"]], it["own_score"], it["address"]["id"],
    ))

    summary = {
        "total_addresses": len(rows),
        "auto_hit_self": len(rows) - len(items),
        "risk_count": len(items),
        CATEGORY_SELF_SCORE_LOW: sum(
            1 for it in items if it["category"] == CATEGORY_SELF_SCORE_LOW),
        CATEGORY_BEST_DRIFTED: sum(
            1 for it in items if it["category"] == CATEGORY_BEST_DRIFTED),
        CATEGORY_MARGIN_AMBIGUOUS: sum(
            1 for it in items if it["category"] == CATEGORY_MARGIN_AMBIGUOUS),
    }

    if category is not None:
        items = [it for it in items if it["category"] == category]

    return {
        "generated_at": _now(),
        **summary,
        "thresholds": {
            "auto_threshold": settings.auto_threshold,
            "margin_threshold": settings.margin_threshold,
            "reject_threshold": settings.reject_threshold,
            "token_weight": settings.token_weight,
        },
        "items": items,
    }
