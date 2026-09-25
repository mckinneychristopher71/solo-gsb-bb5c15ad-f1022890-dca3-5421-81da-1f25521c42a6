"""SQLite persistence layer (standard-library ``sqlite3``, no ORM needed)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS addresses (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    full_text            TEXT NOT NULL,
    tokens_json          TEXT NOT NULL,
    street_tokens_json   TEXT NOT NULL DEFAULT '[]',
    locality_tokens_json TEXT NOT NULL DEFAULT '[]',
    house                TEXT,
    line1                TEXT,
    city                 TEXT,
    state                TEXT,
    postal_code          TEXT,
    country              TEXT,
    external_id          TEXT,
    raw_json             TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_addresses_external_id ON addresses(external_id);

CREATE TABLE IF NOT EXISTS match_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query            TEXT NOT NULL,
    status           TEXT NOT NULL,          -- auto_matched | needs_review | no_match
    score            REAL,
    selected_id      INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    -- Durable snapshot of the human-chosen address, written at review time by
    -- both the single and the same-case-group review paths.  These plain
    -- (non-FK) columns survive the deletion of the address row, so the review
    -- replay can always restore the original ID, full text and review score
    -- instead of falling back to a generic "address deleted" notice.
    selected_address_id   INTEGER,            -- chosen address ID at review time (never nulled by FK)
    selected_address_text TEXT,               -- chosen address full_text at review time
    candidate_id     INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    token_score      REAL,
    house_score      REAL,
    matched_tokens   TEXT,                   -- JSON
    conflicts        TEXT,                   -- JSON
    candidates_json  TEXT NOT NULL,          -- JSON
    consensus_json   TEXT,                   -- JSON: review-consensus verdict at match time
    created_at       TEXT NOT NULL,
    reviewed_at      TEXT,
    review_action    TEXT                   -- confirmed | overridden | rejected
);
CREATE INDEX IF NOT EXISTS idx_match_logs_status ON match_logs(status);

CREATE TABLE IF NOT EXISTS upload_previews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    table_snapshot  TEXT NOT NULL,          -- JSON digest of the address table
    valid_rows_json TEXT NOT NULL,          -- JSON rows that would be written
    valid_count     INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    invalid_count   INTEGER NOT NULL,
    invalid_lines   TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_experiments (
    id              TEXT PRIMARY KEY,       -- EXP-<UTC timestamp>-<random>
    profiles_json   TEXT NOT NULL,          -- rule profiles (A/B/...) in order
    queries_json    TEXT NOT NULL,          -- queries in submitted order
    table_snapshot  TEXT NOT NULL,          -- JSON digest of the frozen address table
    address_count   INTEGER NOT NULL,
    results_json    TEXT NOT NULL,          -- per-query, per-profile results + diffs
    summary_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- Manual-review claims: a reviewer "checks out" one pending record for a
-- short lease.  At most one row per match_id is *active* at any moment
-- (active = released_at IS NULL AND expires_at > now); that invariant is
-- enforced by the claim transaction in app/review_claims.py.  Old rows are
-- kept as an audit trail of who held which record.
CREATE TABLE IF NOT EXISTS review_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL REFERENCES match_logs(id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE,       -- bearer token required at review time
    reviewer    TEXT NOT NULL,              -- display name typed by the reviewer
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,              -- lease end (created_at + lease seconds)
    released_at TEXT                        -- set on manual release or review submit
);
CREATE INDEX IF NOT EXISTS idx_review_claims_match ON review_claims(match_id);
"""


def connect() -> sqlite3.Connection:
    settings = get_settings()
    db_file = Path(settings.db_path)
    if not db_file.is_absolute():
        db_file = Path.cwd() / db_file
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Lightweight migrations for databases created before the column existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(match_logs)")}
    if "consensus_json" not in cols:
        conn.execute("ALTER TABLE match_logs ADD COLUMN consensus_json TEXT")
    if "selected_address_id" not in cols:
        conn.execute("ALTER TABLE match_logs ADD COLUMN selected_address_id INTEGER")
    if "selected_address_text" not in cols:
        conn.execute("ALTER TABLE match_logs ADD COLUMN selected_address_text TEXT")
    # One-time backfill for reviews written before the snapshot existed: the
    # durable ID/text are copied from the still-live FK row where possible.
    # Records whose chosen address was already deleted keep NULL snapshots and
    # are reported as "original choice unknown" by the replay -- never guessed
    # from the candidate list.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(match_logs)")}
    if "selected_address_id" in cols:
        conn.execute(
            """
            UPDATE match_logs
               SET selected_address_id = selected_id,
                   selected_address_text = (
                       SELECT a.full_text FROM addresses a
                        WHERE a.id = match_logs.selected_id
                   )
             WHERE status = 'reviewed'
               AND review_action IN ('confirmed', 'overridden')
               AND selected_id IS NOT NULL
               AND selected_address_id IS NULL
            """
        )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Open a write transaction up-front (BEGIN IMMEDIATE).

    Acquiring the write lock before any reads guarantees that a table snapshot
    taken inside the block is still the same snapshot the INSERTs commit against
    -- another connection cannot mutate the table in between.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- addresses --

ADDRESS_FIELDS = ("line1", "city", "state", "postal_code", "country", "external_id")


def insert_address(conn: sqlite3.Connection, *, full_text: str, tokens: list[str],
                   house: str | None, fields: dict[str, Any],
                   raw: dict[str, Any],
                   street_tokens: list[str] | None = None,
                   locality_tokens: list[str] | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO addresses
            (full_text, tokens_json, street_tokens_json, locality_tokens_json,
             house, line1, city, state, postal_code, country, external_id,
             raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            full_text,
            json.dumps(tokens, ensure_ascii=False),
            json.dumps(street_tokens if street_tokens is not None else tokens,
                       ensure_ascii=False),
            json.dumps(locality_tokens or [], ensure_ascii=False),
            house,
            fields.get("line1"),
            fields.get("city"),
            fields.get("state"),
            fields.get("postal_code"),
            fields.get("country"),
            fields.get("external_id"),
            json.dumps(raw, ensure_ascii=False),
            _now(),
        ),
    )
    return int(cur.lastrowid)


def count_addresses(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0])


def get_address(conn: sqlite3.Connection, address_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()


def list_addresses(conn: sqlite3.Connection, limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM addresses ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def all_addresses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM addresses ORDER BY id").fetchall()


def address_exists(conn: sqlite3.Connection, full_text: str, external_id: str | None) -> bool:
    if external_id:
        row = conn.execute(
            "SELECT 1 FROM addresses WHERE external_id = ? OR full_text = ? LIMIT 1",
            (external_id, full_text),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM addresses WHERE full_text = ? LIMIT 1", (full_text,)
        ).fetchone()
    return row is not None


def address_table_snapshot(conn: sqlite3.Connection) -> str:
    """Stable digest of the current contents of the addresses table.

    Combines the row count, the maximum id and a hash over every row in id
    order, so any insert/delete changes the snapshot (ids are never reused).
    Must be called inside a write transaction when used for confirm validation.
    """
    rows = conn.execute("SELECT id, full_text, external_id FROM addresses ORDER BY id").fetchall()
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(f"{row['id']}\x1f{row['full_text']}\x1f{row['external_id'] or ''}\n".encode("utf-8"))
    return json.dumps(
        {"count": len(rows), "max_id": rows[-1]["id"] if rows else 0,
         "digest": hasher.hexdigest()},
        ensure_ascii=False,
    )


# ------------------------------------------------------------- upload previews --

def insert_upload_preview(conn: sqlite3.Connection, *, token: str, filename: str,
                          file_sha256: str, table_snapshot: str,
                          valid_rows: list[dict[str, Any]], valid_count: int,
                          duplicate_count: int, invalid_count: int,
                          invalid_lines: list[int], ttl_seconds: int) -> None:
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)
    conn.execute(
        """
        INSERT INTO upload_previews
            (token, filename, file_sha256, table_snapshot, valid_rows_json,
             valid_count, duplicate_count, invalid_count, invalid_lines,
             created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token, filename, file_sha256, table_snapshot,
            json.dumps(valid_rows, ensure_ascii=False),
            valid_count, duplicate_count, invalid_count,
            json.dumps(invalid_lines),
            now.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
        ),
    )


def get_upload_preview(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM upload_previews WHERE token = ?", (token,)
    ).fetchone()


def delete_upload_preview(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM upload_previews WHERE token = ?", (token,))


def delete_expired_previews(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM upload_previews WHERE expires_at < ?", (_now(),)
    )


# ---------------------------------------------------------------- match logs --

def insert_match_log(conn: sqlite3.Connection, *, query: str, status: str,
                     score: float | None, candidate_id: int | None,
                     token_score: float | None, house_score: float | None,
                     matched_tokens: list[dict[str, Any]],
                     conflicts: list[dict[str, Any]],
                     candidates: list[dict[str, Any]],
                     consensus: dict[str, Any] | None = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO match_logs
            (query, status, score, candidate_id, token_score, house_score,
             matched_tokens, conflicts, candidates_json, consensus_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query,
            status,
            score,
            candidate_id,
            token_score,
            house_score,
            json.dumps(matched_tokens, ensure_ascii=False),
            json.dumps(conflicts, ensure_ascii=False),
            json.dumps(candidates, ensure_ascii=False),
            json.dumps(consensus, ensure_ascii=False) if consensus else None,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def list_match_logs(conn: sqlite3.Connection, status: str | None = None,
                    limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT * FROM match_logs WHERE status = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM match_logs ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def get_match_log(conn: sqlite3.Connection, log_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM match_logs WHERE id = ?", (log_id,)).fetchone()


def review_match_log(conn: sqlite3.Connection, log_id: int, *, action: str,
                     selected_id: int | None, score: float | None,
                     selected_text: str | None = None) -> None:
    """Write a human review decision onto a match log.

    For positive decisions (``confirmed`` / ``overridden``) the chosen
    address's ID and full text are also stored in the durable snapshot
    columns ``selected_address_id`` / ``selected_address_text`` (plain
    integers/strings with no foreign key, so they are not cleared when the
    address row is later deleted).  ``rejected`` clears both the FK column
    and the snapshot.
    """
    if action in ("confirmed", "overridden"):
        durable_id, durable_text = selected_id, selected_text
    else:
        durable_id = durable_text = None
    conn.execute(
        """
        UPDATE match_logs
           SET status = 'reviewed', review_action = ?, selected_id = ?,
               selected_address_id = ?, selected_address_text = ?,
               score = ?, reviewed_at = ?
         WHERE id = ?
        """,
        (action, selected_id, durable_id, durable_text, score, _now(), log_id),
    )


def count_match_logs(conn: sqlite3.Connection, status: str | None = None) -> int:
    if status:
        return int(
            conn.execute("SELECT COUNT(*) FROM match_logs WHERE status = ?", (status,))
            .fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM match_logs").fetchone()[0])


# -------------------------------------------------------------- review claims --


def insert_claim(conn: sqlite3.Connection, *, match_id: int, token: str,
                 reviewer: str, ttl_seconds: int) -> str:
    """Insert a claim row; returns its ``expires_at`` (lease end) timestamp."""
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)
    expires_at = expires.isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO review_claims
            (match_id, token, reviewer, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (match_id, token, reviewer, now.isoformat(timespec="seconds"), expires_at),
    )
    return expires_at


def get_claim_by_token(conn: sqlite3.Connection, match_id: int,
                       token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM review_claims WHERE match_id = ? AND token = ?",
        (match_id, token),
    ).fetchone()


def get_active_claim(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    """The currently valid claim for a record (unreleased and unexpired)."""
    return conn.execute(
        """
        SELECT * FROM review_claims
         WHERE match_id = ? AND released_at IS NULL AND expires_at > ?
         ORDER BY id DESC LIMIT 1
        """,
        (match_id, _now()),
    ).fetchone()


def active_claim_map(conn: sqlite3.Connection,
                     match_ids: list[int] | None = None) -> dict[int, sqlite3.Row]:
    """``{match_id: claim_row}`` for every currently active claim.

    With ``match_ids`` given, only those records are looked up (one query).
    """
    now = _now()
    if match_ids is not None:
        if not match_ids:
            return {}
        marks = ",".join("?" for _ in match_ids)
        rows = conn.execute(
            f"""
            SELECT * FROM review_claims
             WHERE released_at IS NULL AND expires_at > ? AND match_id IN ({marks})
            """,
            (now, *match_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_claims WHERE released_at IS NULL AND expires_at > ?",
            (now,),
        ).fetchall()
    return {row["match_id"]: row for row in rows}


def release_claim(conn: sqlite3.Connection, claim_id: int) -> None:
    """Close a claim (manual release or consumed by a submitted review)."""
    conn.execute(
        "UPDATE review_claims SET released_at = ? WHERE id = ?",
        (_now(), claim_id),
    )


# ----------------------------------------------------------- A/B experiments --


def insert_ab_experiment(conn: sqlite3.Connection, *, experiment_id: str,
                         profiles: list[dict[str, Any]], queries: list[str],
                         table_snapshot: str, address_count: int,
                         results: list[dict[str, Any]],
                         summary: dict[str, Any], created_at: str) -> None:
    conn.execute(
        """
        INSERT INTO ab_experiments
            (id, profiles_json, queries_json, table_snapshot, address_count,
             results_json, summary_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            experiment_id,
            json.dumps(profiles, ensure_ascii=False),
            json.dumps(queries, ensure_ascii=False),
            table_snapshot,
            address_count,
            json.dumps(results, ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
            created_at,
        ),
    )


def get_ab_experiment(conn: sqlite3.Connection, experiment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ab_experiments WHERE id = ?", (experiment_id,)
    ).fetchone()


def list_ab_experiments(conn: sqlite3.Connection, limit: int = 50,
                        offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, profiles_json, queries_json, table_snapshot, address_count,"
        " summary_json, created_at FROM ab_experiments"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def count_ab_experiments(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ab_experiments").fetchone()[0])
