"""导入影响预演 (import impact dry-run).

Before committing a CSV preview, this module rehearses the import **in
memory**: the voucher's valid rows are normalised with the very same row
normalisation the real import uses (``ingest.normalize_for_insert``) and
appended to a temporary copy of the address table -- nothing is written.

The rehearsal then takes the **most recent 100 distinct original queries**
from the match logs (walked newest-first) and re-runs each of them twice
along the existing matching + review-consensus logic:

* **before** -- against the current address table;
* **after**  -- against the temporary merged table (current + new rows).

New addresses have no database id (they are never inserted); they are
identified by their **CSV line number** (``csv:<line_no>`` sentinel ids).

Each query is classified by comparing the two verdicts:

* ``improved``   改善   -- status rose (no_match < needs_review < auto_matched);
* ``regressed``  退化   -- status fell;
* ``retargeted`` 目标改派 -- both runs auto-hit, but the target differs;
* ``unchanged``  不变   -- anything else.

Invalid and duplicate CSV rows never participate (they are not in the
voucher's valid-row list), and the dry-run writes neither addresses nor
match logs: the only reads are the voucher, the address table snapshot and
the match/review history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import consensus, db, ingest
from .config import Settings
from .matcher import match_address

# At most this many distinct logged queries are rehearsed (newest first).
IMPACT_QUERY_LIMIT = 100

CATEGORY_IMPROVED = "improved"
CATEGORY_REGRESSED = "regressed"
CATEGORY_RETARGETED = "retargeted"
CATEGORY_UNCHANGED = "unchanged"

VALID_CATEGORIES = (
    CATEGORY_IMPROVED,
    CATEGORY_REGRESSED,
    CATEGORY_RETARGETED,
    CATEGORY_UNCHANGED,
)

CATEGORY_LABELS = {
    CATEGORY_IMPROVED: "改善",
    CATEGORY_REGRESSED: "退化",
    CATEGORY_RETARGETED: "目标改派",
    CATEGORY_UNCHANGED: "不变",
}

_STATUS_LABELS = {
    "auto_matched": "自动命中",
    "needs_review": "待确认",
    "no_match": "无匹配",
}

# Status quality ladder: a higher rung after the import is an improvement.
_STATUS_RANK = {"no_match": 0, "needs_review": 1, "auto_matched": 2}

# Display order: regressions are the risk an import review must see first,
# then target re-assignments, improvements, and finally unchanged queries.
_CATEGORY_RANK = {
    CATEGORY_REGRESSED: 0,
    CATEGORY_RETARGETED: 1,
    CATEGORY_IMPROVED: 2,
    CATEGORY_UNCHANGED: 3,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_voucher(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    """Validate the preview voucher for a dry-run (read-only variant of the
    confirm checks: token exists, is unexpired and the address-table snapshot
    is unchanged).  Unlike confirm, nothing is deleted or written -- an
    expired voucher is simply reported, never cleaned up here.
    """
    voucher = db.get_upload_preview(conn, token)
    if voucher is None:
        raise ingest.PreviewExpired("预览凭证不存在，须重新预览")
    if voucher["expires_at"] < _now():
        raise ingest.PreviewExpired("预览已过期，须重新预览")
    if db.address_table_snapshot(conn) != voucher["table_snapshot"]:
        raise ingest.PreviewTableChanged("标准地址表在预览后发生变化，须重新预览")
    return voucher


def _recent_distinct_queries(conn: sqlite3.Connection,
                             limit: int = IMPACT_QUERY_LIMIT
                             ) -> list[tuple[str, int]]:
    """The most recent ``limit`` distinct original queries from the match
    logs, newest log first.  Returns ``(query, latest_match_id)`` pairs.
    """
    rows = conn.execute(
        "SELECT id, query FROM match_logs ORDER BY id DESC"
    ).fetchall()
    seen: set[str] = set()
    picked: list[tuple[str, int]] = []
    for row in rows:
        query = row["query"]
        if query in seen:
            continue
        seen.add(query)
        picked.append((query, row["id"]))
        if len(picked) >= limit:
            break
    return picked


def _temp_address_rows(valid_rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Build matcher-compatible in-memory rows for the voucher's valid rows.

    The normalisation is exactly the importer's (``normalize_for_insert``);
    the synthetic ``id`` carries the CSV line number so a rehearsal hit on a
    not-yet-imported address is always attributable to its source line.
    """
    rows: list[dict[str, Any]] = []
    for line_no, raw in valid_rows:
        prepared = ingest.normalize_for_insert(raw)
        rows.append({
            "id": f"csv:{line_no}",
            "csv_line": line_no,
            "full_text": prepared["full_text"],
            "tokens_json": json.dumps(prepared["tokens"], ensure_ascii=False),
            "street_tokens_json": json.dumps(prepared["street_tokens"],
                                             ensure_ascii=False),
            "locality_tokens_json": json.dumps(prepared["locality_tokens"],
                                               ensure_ascii=False),
            "house": prepared["house"],
            "line1": prepared["fields"].get("line1"),
            "city": prepared["fields"].get("city"),
            "state": prepared["fields"].get("state"),
            "postal_code": prepared["fields"].get("postal_code"),
            "country": prepared["fields"].get("country"),
            "external_id": prepared["fields"].get("external_id"),
            "raw_json": json.dumps(prepared["raw"], ensure_ascii=False),
        })
    return rows


def _run_verdict(conn: sqlite3.Connection, query: str, rows: list[Any],
                 settings: Settings) -> dict[str, Any]:
    """One rehearsal run: the existing matcher plus the review-consensus
    memory, exactly like ``/api/match`` -- but nothing is persisted.
    """
    result = match_address(query, rows, settings)
    verdict = consensus.lookup_consensus(conn, query)
    consensus.apply_consensus(result, verdict)

    best = result.best
    # The "auto-hit target" only exists when the verdict actually auto-hits;
    # for needs_review / no_match the best candidate is not an accepted target.
    target: dict[str, Any] | None = None
    target_key: tuple[str, Any] | None = None
    if best is not None and result.status == "auto_matched":
        raw_id = best.address["id"]
        if isinstance(raw_id, str) and raw_id.startswith("csv:"):
            # A not-yet-imported address: identify it by its CSV line number.
            csv_line = int(raw_id.split(":", 1)[1])
            target = {"kind": "new", "csv_line": csv_line,
                      "full_text": best.address["full_text"]}
            target_key = ("new", csv_line)
        else:
            target = {"kind": "existing", "id": raw_id,
                      "full_text": best.address["full_text"]}
            target_key = ("existing", raw_id)

    return {
        "status": result.status,
        "status_label": _STATUS_LABELS.get(result.status, result.status),
        "reason": result.reason,
        "score": best.score if best else None,
        "target": target,
        "target_key": target_key,
        "consensus_applied": bool(
            result.consensus and result.consensus.get("applied")
        ),
    }


def _target_text(target: dict[str, Any] | None) -> str:
    if target is None:
        return "无自动命中目标"
    if target["kind"] == "new":
        return f"CSV 第 {target['csv_line']} 行「{target['full_text']}」"
    return f"#{target['id']}「{target['full_text']}」"


def _classify(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
    """Impact category + human-readable reason for one query."""
    b_label = before["status_label"]
    a_label = after["status_label"]
    b_rank = _STATUS_RANK[before["status"]]
    a_rank = _STATUS_RANK[after["status"]]
    if a_rank > b_rank:
        return CATEGORY_IMPROVED, (
            f"状态改善：{b_label} → {a_label}"
            f"（导入后目标：{_target_text(after['target'])}）"
        )
    if a_rank < b_rank:
        return CATEGORY_REGRESSED, (
            f"状态退化：{b_label} → {a_label}"
            f"（导入前目标：{_target_text(before['target'])}）"
        )
    if (before["status"] == "auto_matched"
            and before["target_key"] != after["target_key"]):
        return CATEGORY_RETARGETED, (
            f"两次均自动命中，但目标由 {_target_text(before['target'])} "
            f"改派为 {_target_text(after['target'])}"
        )
    if before["status"] == "auto_matched":
        return CATEGORY_UNCHANGED, (
            f"状态与自动命中目标均未变化（{_target_text(after['target'])}）"
        )
    return CATEGORY_UNCHANGED, f"状态未变化（{a_label}），无自动命中目标改派"


def compute_impact(conn: sqlite3.Connection, settings: Settings,
                   token: str) -> dict[str, Any]:
    """Rehearse a previewed CSV import against the recent logged queries.

    Raises ``ingest.PreviewExpired`` / ``ingest.PreviewTableChanged`` when the
    voucher cannot be honoured.  Purely in-memory: no address row and no
    match log is ever written, and the voucher stays valid for a later
    confirm.
    """
    voucher = _load_voucher(conn, token)
    valid_rows = json.loads(voucher["valid_rows_json"])
    temp_rows = _temp_address_rows(valid_rows)

    # One snapshot of the current table is shared by every "before" run; the
    # merged table (current rows in id order, then the new rows in CSV line
    # order -- exactly how all_addresses would return them after a real
    # import) backs every "after" run.
    current_rows = list(db.all_addresses(conn))
    merged_rows = current_rows + temp_rows

    queries = _recent_distinct_queries(conn)

    items: list[dict[str, Any]] = []
    for query, latest_match_id in queries:
        before = _run_verdict(conn, query, current_rows, settings)
        after = _run_verdict(conn, query, merged_rows, settings)
        category, reason = _classify(before, after)
        items.append({
            "query": query,
            "latest_match_id": latest_match_id,
            "before": {k: v for k, v in before.items() if k != "target_key"},
            "after": {k: v for k, v in after.items() if k != "target_key"},
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "reason": reason,
        })

    items.sort(key=lambda it: (_CATEGORY_RANK[it["category"]],
                               -it["latest_match_id"]))

    summary = {
        "total": len(items),
        **{cat: sum(1 for it in items if it["category"] == cat)
           for cat in VALID_CATEGORIES},
    }

    return {
        "generated_at": _now(),
        "filename": voucher["filename"],
        "preview_created_at": voucher["created_at"],
        "preview_expires_at": voucher["expires_at"],
        "valid_rows": voucher["valid_count"],
        "new_addresses": [
            {
                "csv_line": row["csv_line"],
                "full_text": row["full_text"],
                "house": row["house"],
                "external_id": row["external_id"],
            }
            for row in temp_rows
        ],
        "query_limit": IMPACT_QUERY_LIMIT,
        "query_count": len(items),
        "summary": summary,
        "thresholds": {
            "auto_threshold": settings.auto_threshold,
            "margin_threshold": settings.margin_threshold,
            "reject_threshold": settings.reject_threshold,
            "token_weight": settings.token_weight,
        },
        "items": items,
    }
