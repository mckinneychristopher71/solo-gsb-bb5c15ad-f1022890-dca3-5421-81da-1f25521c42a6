"""Tests for the standard-address self-match audit (``/api/address-audit``)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _add(client, *full_texts: str) -> None:
    r = client.post("/api/addresses", json={
        "addresses": [{"address": t} for t in full_texts],
        "skip_duplicates": True,
    })
    assert r.status_code == 200
    assert r.json()["inserted"] == len(full_texts), r.json()


def _audit(client, category: str | None = None) -> dict:
    qs = f"?category={category}" if category else ""
    r = client.get(f"/api/address-audit{qs}")
    assert r.status_code == 200
    return r.json()


def _item_for(data: dict, full_text: str) -> dict:
    matches = [it for it in data["items"] if it["address"]["full_text"] == full_text]
    assert len(matches) == 1, [
        it["address"]["full_text"] for it in data["items"]
    ]
    return matches[0]


def test_clean_sample_table_reports_no_risk():
    # A throwaway database holding only the bundled sample addresses: every
    # record auto-hits its own ID, so the audit is empty.
    import os
    import tempfile
    from pathlib import Path

    from app import address_audit, db
    from app import ingest as ingest_mod
    from app.config import get_settings

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    old = os.environ.get("ADDRESS_DB_PATH")
    os.environ["ADDRESS_DB_PATH"] = path
    conn = None
    try:
        conn = db.connect()
        db.init_db(conn)
        sample = Path(get_settings().sample_csv)
        ingest_mod.add_many(conn, ingest_mod.parse_csv(sample.read_bytes()))
        out = address_audit.compute_audit(conn, get_settings())
        assert out["total_addresses"] == 15
        assert out["auto_hit_self"] == 15
        assert out["risk_count"] == 0
        assert out["self_score_low"] == 0
        assert out["best_drifted"] == 0
        assert out["margin_ambiguous"] == 0
        assert out["items"] == []
        assert out["thresholds"]["margin_threshold"] == pytest.approx(0.06)
    finally:
        if conn is not None:
            conn.close()
        if old is None:
            os.environ.pop("ADDRESS_DB_PATH", None)
        else:
            os.environ["ADDRESS_DB_PATH"] = old
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def test_near_duplicate_flags_best_drifted_and_margin_ambiguous(client):
    # Two spellings that normalise identically: the earlier row stays first
    # on the tie, so the later row's best choice drifts away from itself,
    # while the earlier row only suffers the zero-margin ambiguity.
    _add(client, "100 Zxqwv Pine Rd, Tstville 99001",
         "100 Zxqwv Pine Road, Tstville 99001")
    data = _audit(client)

    later = _item_for(data, "100 Zxqwv Pine Road, Tstville 99001")
    assert later["category"] == "best_drifted"
    assert later["category_label"] == "首选偏移"
    assert later["own_score"] == 1.0
    assert later["best_id"] != later["address"]["id"]
    assert later["best_full_text"] == "100 Zxqwv Pine Rd, Tstville 99001"
    twin = [b for b in later["blockers"]
            if b["address"]["full_text"] == "100 Zxqwv Pine Rd, Tstville 99001"]
    assert len(twin) == 1 and twin[0]["score"] == 1.0

    earlier = _item_for(data, "100 Zxqwv Pine Rd, Tstville 99001")
    assert earlier["category"] == "margin_ambiguous"
    assert earlier["best_id"] == earlier["address"]["id"]
    assert earlier["own_score"] == 1.0


def test_margin_ambiguous_when_lead_too_small(client):
    # Identical house number and a 6-vs-7-token street: the competitor scores
    # ~0.946, inside the 0.06 margin of the perfect self-score.
    _add(client, "7 Alpha Beta Gamma Delta Epsilon Rd",
         "7 Alpha Beta Gamma Delta Epsilon Zeta Rd")
    data = _audit(client)
    for text in ("7 Alpha Beta Gamma Delta Epsilon Rd",
                 "7 Alpha Beta Gamma Delta Epsilon Zeta Rd"):
        item = _item_for(data, text)
        assert item["category"] == "margin_ambiguous"
        assert item["own_score"] == 1.0
        assert item["best_id"] == item["address"]["id"]
        assert item["match_status"] == "needs_review"
        assert len(item["blockers"]) == 1
        assert item["blockers"][0]["score"] >= 0.94


def test_self_score_low_for_degenerate_row(client):
    # A row whose whole text is just a house number cannot score highly
    # against anything, including itself.
    _add(client, "100")
    data = _audit(client)
    item = _item_for(data, "100")
    assert item["category"] == "self_score_low"
    assert item["category_label"] == "自身分数不足"
    assert item["own_score"] < 0.80
    assert item["match_status"] == "no_match"
    # "100 Lake Drive" shares the house number and clears the blocker floor.
    blocker_texts = {b["address"]["full_text"] for b in item["blockers"]}
    assert any("100 Lake Drive" in t for t in blocker_texts)
    assert all(b["score"] >= item["blocker_floor"] for b in item["blockers"])


def test_blockers_not_truncated_to_top5(client):
    # Seven spelling variants all score 1.0 against each other. For the last
    # inserted row the other six are blocking candidates -- more than the
    # classic top-5 candidate cut would ever show (the row itself would not
    # even appear in its own top 5).
    variants = [
        "200 Qvbnm Oak Ln",
        "200 Qvbnm Oak Lane",
        "200 Qvbnm oak ln",
        "200 QVBNM OAK LN",
        "200 Qvbnm Oak Ln.",
        "200 Qvbnm Oak  Ln",
        "200 qvbnm oak lane",
    ]
    _add(client, *variants)
    data = _audit(client)
    last = _item_for(data, "200 qvbnm oak lane")
    assert last["category"] == "best_drifted"
    assert last["own_score"] == 1.0
    assert len(last["blockers"]) == 6
    assert all(b["score"] == 1.0 for b in last["blockers"])
    blocker_ids = {b["address"]["id"] for b in last["blockers"]}
    assert last["address"]["id"] not in blocker_ids
    assert last["best_id"] in blocker_ids


def test_upload_is_reflected_on_next_call(client):
    # The audit recomputes from the live table on every call: duplicating a
    # previously clean sample address makes both rows appear as risks.
    before = _audit(client)
    assert not any("Oak Avenue" in it["address"]["full_text"]
                   for it in before["items"])
    _add(client, "456 Oak Ave, Brooklyn, NY 11201")
    after = _audit(client)
    drifted = _item_for(after, "456 Oak Ave, Brooklyn, NY 11201")
    assert drifted["category"] == "best_drifted"
    assert drifted["best_full_text"] == "456 Oak Avenue"
    original = _item_for(after, "456 Oak Avenue")
    assert original["category"] == "margin_ambiguous"


def test_category_filter_keeps_summary_counts(client):
    full = _audit(client)
    assert full["risk_count"] > 0
    for cat in ("best_drifted", "self_score_low", "margin_ambiguous"):
        filtered = _audit(client, cat)
        assert all(it["category"] == cat for it in filtered["items"])
        # summary counts are independent of the filter
        assert filtered["risk_count"] == full["risk_count"]
        assert filtered["total_addresses"] == full["total_addresses"]
        assert filtered["auto_hit_self"] == full["auto_hit_self"]
        assert filtered[cat] == full[cat]
    assert full["risk_count"] == (
        full["best_drifted"] + full["self_score_low"] + full["margin_ambiguous"]
    )
    assert full["auto_hit_self"] == full["total_addresses"] - full["risk_count"]


def test_invalid_category_filter(client):
    r = client.get("/api/address-audit", params={"category": "bogus"})
    assert r.status_code == 400


def test_audit_is_read_only(client):
    logs_before = client.get("/api/matches?limit=1").json()["total"]
    _audit(client)
    _audit(client, "best_drifted")
    logs_after = client.get("/api/matches?limit=1").json()["total"]
    assert logs_after == logs_before
