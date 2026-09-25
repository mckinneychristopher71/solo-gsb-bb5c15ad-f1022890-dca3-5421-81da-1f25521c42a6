"""Helpers for building/inserting standard addresses from CSV or JSON."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db
from .normalization import normalize_parts

# Accepted CSV header aliases -> canonical field name.
_FIELD_ALIASES: dict[str, str] = {
    "address": "full_text",
    "addr": "full_text",
    "formatted_address": "full_text",
    "full_address": "full_text",
    "full_text": "full_text",
    "standard_address": "full_text",
    "line1": "line1",
    "street": "line1",
    "address_line1": "line1",
    "city": "city",
    "town": "city",
    "state": "state",
    "province": "state",
    "region": "state",
    "postal_code": "postal_code",
    "postcode": "postal_code",
    "zip": "postal_code",
    "zipcode": "postal_code",
    "country": "country",
    "external_id": "external_id",
    "id": "external_id",
    "code": "external_id",
}

_ADDRESS_KEYS = ("line1", "city", "state", "postal_code", "country")

# Preview tokens stay valid for this long; afterwards the client must re-preview.
PREVIEW_TTL_SECONDS = 30 * 60


class PreviewError(Exception):
    """Raised when a preview-confirm request cannot be honoured."""


class PreviewExpired(PreviewError):
    """The preview token does not exist or has expired."""


class PreviewFileMismatch(PreviewError):
    """The re-submitted file differs from the one bound to the preview."""


class PreviewTableChanged(PreviewError):
    """The address table changed since the preview was created."""


@dataclass
class PlannedRow:
    """One CSV row classified by the preview planner."""
    line_no: int
    status: str                 # valid | duplicate | invalid
    row: dict[str, Any] = field(default_factory=dict)
    full_text: str = ""
    external_id: str | None = None


def build_full_text(raw: dict[str, Any]) -> str:
    """Compose the comparable address string from structured parts."""
    explicit = raw.get("full_text")
    if explicit:
        return str(explicit).strip()
    parts = [
        raw.get("line1"),
        raw.get("city"),
        raw.get("state"),
        raw.get("postal_code"),
        raw.get("country"),
    ]
    return ", ".join(str(p).strip() for p in parts if p).strip()


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map arbitrary header aliases to canonical fields; strip blanks."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key is None:
            continue
        canonical = _FIELD_ALIASES.get(str(key).strip().lower())
        if canonical and value is not None and str(value).strip() != "":
            out.setdefault(canonical, str(value).strip())
    out["full_text"] = build_full_text(out)
    return out


def normalize_for_insert(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one raw row exactly as the importer does.

    Returns the keyword bundle accepted by ``db.insert_address``; the import
    impact dry-run builds its in-memory temporary addresses from the same
    bundle, so the rehearsal always matches what a real confirm would write.
    """
    norm_row = normalize_row(raw)
    normalized = normalize_parts(
        line1=norm_row.get("line1") if not norm_row.get("full_text") else None,
        city=norm_row.get("city"),
        state=norm_row.get("state"),
        postal_code=norm_row.get("postal_code"),
        country=norm_row.get("country"),
        full_text=norm_row.get("full_text"),
    )
    fields = {key: norm_row.get(key) for key in _ADDRESS_KEYS}
    fields["external_id"] = norm_row.get("external_id")
    return {
        "full_text": norm_row["full_text"],
        "tokens": normalized.tokens,
        "street_tokens": normalized.street_tokens,
        "locality_tokens": normalized.locality_tokens,
        "house": normalized.house,
        "fields": fields,
        "raw": norm_row,
    }


def _persist_planned_row(conn: sqlite3.Connection, planned: PlannedRow) -> int:
    """Insert a row already classified as valid (no further dedup check)."""
    return db.insert_address(conn, **normalize_for_insert(planned.row))


def add_address(conn: sqlite3.Connection, raw: dict[str, Any],
                skip_duplicates: bool = True) -> int | None:
    """Normalise and persist one standard address. Returns its id (or None)."""
    norm_row = normalize_row(raw)
    full_text = norm_row["full_text"]
    if not full_text:
        return None
    external_id = norm_row.get("external_id")
    if skip_duplicates and db.address_exists(conn, full_text, external_id):
        return None

    normalized = normalize_parts(
        line1=norm_row.get("line1") if not norm_row.get("full_text") else None,
        city=norm_row.get("city"),
        state=norm_row.get("state"),
        postal_code=norm_row.get("postal_code"),
        country=norm_row.get("country"),
        full_text=norm_row.get("full_text"),
    )

    fields = {key: norm_row.get(key) for key in _ADDRESS_KEYS}
    fields["external_id"] = external_id
    return db.insert_address(
        conn,
        full_text=full_text,
        tokens=normalized.tokens,
        street_tokens=normalized.street_tokens,
        locality_tokens=normalized.locality_tokens,
        house=normalized.house,
        fields=fields,
        raw=norm_row,
    )


def plan_rows(conn: sqlite3.Connection,
              rows: Iterable[tuple[int, dict[str, Any]]],
              skip_duplicates: bool = True) -> list[PlannedRow]:
    """Classify rows as valid / duplicate / invalid without writing anything.

    Duplicate detection mirrors ``db.address_exists`` (a row is a duplicate if
    its full_text or external_id already exists) and also covers repeats inside
    the uploaded file itself, so the preview matches what the confirm writes.
    """
    existing_full = {r["full_text"] for r in conn.execute(
        "SELECT full_text FROM addresses")}
    existing_ext = {r["external_id"] for r in conn.execute(
        "SELECT external_id FROM addresses WHERE external_id IS NOT NULL")}
    seen_full: set[str] = set()
    seen_ext: set[str] = set()
    planned: list[PlannedRow] = []
    for line_no, row in rows:
        norm_row = normalize_row(row)
        full_text = norm_row["full_text"]
        external_id = norm_row.get("external_id")
        if not full_text:
            planned.append(PlannedRow(line_no=line_no, status="invalid"))
            continue
        is_dup = (
            skip_duplicates
            and (
                full_text in existing_full or full_text in seen_full
                or (external_id is not None
                    and (external_id in existing_ext or external_id in seen_ext))
            )
        )
        if is_dup:
            planned.append(PlannedRow(
                line_no=line_no, status="duplicate",
                full_text=full_text, external_id=external_id))
        else:
            planned.append(PlannedRow(
                line_no=line_no, status="valid", row=row,
                full_text=full_text, external_id=external_id))
            seen_full.add(full_text)
            if external_id is not None:
                seen_ext.add(external_id)
    return planned


def add_many(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]],
             skip_duplicates: bool = True) -> dict[str, int]:
    numbered = [(idx, row) for idx, row in enumerate(rows, start=2)]
    planned = plan_rows(conn, numbered, skip_duplicates=skip_duplicates)
    inserted = duplicates = invalid = 0
    with db.transaction(conn):
        for item in planned:
            if item.status == "invalid":
                invalid += 1
            elif item.status == "duplicate":
                duplicates += 1
            else:
                _persist_planned_row(conn, item)
                inserted += 1
    return {"inserted": inserted, "duplicates": duplicates, "invalid": invalid}


def parse_csv(content: bytes) -> list[dict[str, str]]:
    """Parse CSV bytes into header-mapped rows (blank physical lines skipped)."""
    return [row for _, row in parse_csv_numbered(content)]


def parse_csv_numbered(content: bytes) -> list[tuple[int, dict[str, str]]]:
    """Parse CSV bytes, pairing each data row with its 1-based physical line.

    Line 1 is the header, so the first data row is line 2. Fully blank physical
    lines (which the csv module skips entirely) are not counted.
    """
    # utf-8-sig transparently strips a possible BOM from Excel exports.
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    fields = [h.strip() for h in header]
    out: list[tuple[int, dict[str, str]]] = []
    for raw in reader:
        # csv.reader yields [] only for a completely empty physical line.
        if not raw:
            continue
        line_no = reader.line_num
        values = list(raw)
        if len(values) < len(fields):
            values += [""] * (len(fields) - len(values))
        row = {fields[i]: values[i] for i in range(len(fields))}
        out.append((line_no, row))
    return out


# ------------------------------------------------------- preview / confirm --


def preview_csv(conn: sqlite3.Connection, content: bytes, filename: str,
                skip_duplicates: bool = True) -> dict[str, Any]:
    """Create a preview voucher for an uploaded CSV without writing addresses."""
    planned = plan_rows(conn, parse_csv_numbered(content),
                        skip_duplicates=skip_duplicates)
    valid = [p for p in planned if p.status == "valid"]
    duplicates = sum(1 for p in planned if p.status == "duplicate")
    invalid_lines = [p.line_no for p in planned if p.status == "invalid"]

    token = secrets.token_urlsafe(24)
    file_sha256 = hashlib.sha256(content).hexdigest()
    with db.immediate_transaction(conn):
        db.delete_expired_previews(conn)
        snapshot = db.address_table_snapshot(conn)
        db.insert_upload_preview(
            conn,
            token=token,
            filename=filename,
            file_sha256=file_sha256,
            table_snapshot=snapshot,
            valid_rows=[[p.line_no, p.row] for p in valid],
            valid_count=len(valid),
            duplicate_count=duplicates,
            invalid_count=len(invalid_lines),
            invalid_lines=invalid_lines,
            ttl_seconds=PREVIEW_TTL_SECONDS,
        )
    return {
        "preview_token": token,
        "filename": filename,
        "valid": len(valid),
        "duplicates": duplicates,
        "invalid": len(invalid_lines),
        "invalid_lines": invalid_lines,
        "total": len(planned),
    }


def confirm_csv(conn: sqlite3.Connection, content: bytes, token: str) -> dict[str, Any]:
    """Commit a preview, after re-validating file digest and table snapshot.

    Everything happens in one SQLite write transaction: the write lock is
    acquired first, then the voucher, the re-submitted file digest and the
    address-table snapshot are checked before any address row is inserted.
    Only the rows classified as valid *in that preview* are written; a single
    failed check rolls the whole transaction back.
    """
    file_sha256 = hashlib.sha256(content).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.immediate_transaction(conn):
        voucher = db.get_upload_preview(conn, token)
        if voucher is None:
            raise PreviewExpired("预览凭证不存在，须重新预览")
        if voucher["expires_at"] < now:
            db.delete_upload_preview(conn, token)
            raise PreviewExpired("预览已过期，须重新预览")
        if voucher["file_sha256"] != file_sha256:
            raise PreviewFileMismatch("重新提交的文件与预览时不一致，须重新预览")
        if db.address_table_snapshot(conn) != voucher["table_snapshot"]:
            raise PreviewTableChanged("标准地址表在预览后发生变化，须重新预览")

        inserted = 0
        for line_no, row in json.loads(voucher["valid_rows_json"]):
            # Rows were already classified valid at preview time; the unchanged
            # snapshot guarantees they are still unique against the table.
            _persist_planned_row(
                conn, PlannedRow(line_no=line_no, status="valid", row=row))
            inserted += 1
        db.delete_upload_preview(conn, token)
        return {
            "inserted": inserted,
            "duplicates": voucher["duplicate_count"],
            "invalid": voucher["invalid_count"],
            "invalid_lines": json.loads(voucher["invalid_lines"]),
            "filename": voucher["filename"],
        }
