"""地址覆盖缺口 (address coverage gaps).

Saved match logs whose status is ``no_match`` describe external addresses
the standard table could not cover at the time of matching. This module
builds a gap manifest from those logs:

* records are grouped by the *normalised* identity of the query -- road
  (street) tokens, locality tokens and house number, exactly like the
  review-consensus key; a different house number is therefore a different
  gap and two keys are never merged across it;
* every group counts as **one** gap no matter how many records fall in it;
  the group carries the occurrence count, distinct raw-query examples and
  the most recent occurrence time;
* whenever the manifest is opened, each group's representative query is
  re-run against the **current** standard address table with the
  **default** rules (the configured thresholds -- plain scoring, the
  review-consensus memory is not consulted). A representative that now
  reaches ``auto_matched`` marks the whole group as **covered** (已覆盖,
  showing the target address); every other verdict stays **open**
  (待补录) and shows the current preferred candidate.

Nothing here writes to the database: the original match logs and the
scoring rules are never modified or re-scored in place.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import Settings
from .consensus import norm_key
from .matcher import match_address, result_to_dict

# At most this many distinct raw queries are echoed back as examples; the
# occurrence count always reflects every record in the group.
EXAMPLE_LIMIT = 5

VALID_COVERAGE = ("open", "covered")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gap_group_key(key: dict[str, Any]) -> str:
    """Stable identity of a gap bucket: normalised tokens + house number."""
    payload = json.dumps(key, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_gaps(conn: sqlite3.Connection, settings: Settings,
                 coverage: str | None = None) -> dict[str, Any]:
    """Build the up-to-date gap manifest from saved ``no_match`` logs.

    ``coverage`` optionally restricts the returned groups to ``open`` or
    ``covered``; the summary counts always cover every group. The re-run
    happens on every call, so a freshly uploaded address flips a group to
    "covered" the next time the manifest is opened.
    """
    rows = conn.execute(
        "SELECT * FROM match_logs WHERE status = 'no_match' ORDER BY id"
    ).fetchall()
    # One snapshot of the current table is shared by every representative
    # re-run in this call.
    addresses = db.all_addresses(conn)

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        nkey = norm_key(row["query"])
        key = _gap_group_key(nkey)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "group_key": key,
                "street_tokens": nkey["street"],
                "locality_tokens": nkey["locality"],
                "house": nkey["house"] or None,
                "records": [],
            }
            buckets[key] = bucket
        bucket["records"].append(row)

    groups: list[dict[str, Any]] = []
    for bucket in buckets.values():
        records = bucket["records"]
        # The earliest record is a stable representative for the bucket;
        # every member normalises to the same key anyway.
        representative = records[0]

        distinct_queries = list(dict.fromkeys(r["query"] for r in records))
        # Keep one record id per distinct query (its first occurrence).
        example_ids: dict[str, int] = {}
        for r in records:
            if r["query"] not in example_ids:
                example_ids[r["query"]] = r["id"]
        examples = [
            {"id": example_ids[q], "query": q}
            for q in distinct_queries[:EXAMPLE_LIMIT]
        ]

        # Re-run with the live table and default rules. Pure scoring only:
        # no review-consensus lookup, and nothing is persisted -- the
        # original no_match log keeps its saved status.
        result = match_address(representative["query"], addresses, settings)
        rerun = result_to_dict(result)
        state = "covered" if result.status == "auto_matched" else "open"

        groups.append({
            "group_key": bucket["group_key"],
            "street_tokens": bucket["street_tokens"],
            "locality_tokens": bucket["locality_tokens"],
            "house": bucket["house"],
            "count": len(records),
            "distinct_query_count": len(distinct_queries),
            "example_truncated": len(distinct_queries) > EXAMPLE_LIMIT,
            "match_ids": [r["id"] for r in records],
            "representative_match_id": representative["id"],
            "representative_query": representative["query"],
            "examples": examples,
            "first_seen_at": records[0]["created_at"],
            "last_seen_at": max(r["created_at"] for r in records),
            "coverage": state,
            "rerun": rerun,
        })

    summary = {
        "total_groups": len(groups),
        "open": sum(1 for g in groups if g["coverage"] == "open"),
        "covered": sum(1 for g in groups if g["coverage"] == "covered"),
        "source_records": len(rows),
    }

    if coverage is not None:
        groups = [g for g in groups if g["coverage"] == coverage]

    # Open gaps first (those are the ones still needing an upload), then
    # the most frequent and most recently seen groups. Stable multi-sort.
    groups.sort(key=lambda g: g["group_key"])
    groups.sort(key=lambda g: g["last_seen_at"], reverse=True)
    groups.sort(key=lambda g: (0 if g["coverage"] == "open" else 1, -g["count"]))

    return {"generated_at": _now(), **summary, "groups": groups}
