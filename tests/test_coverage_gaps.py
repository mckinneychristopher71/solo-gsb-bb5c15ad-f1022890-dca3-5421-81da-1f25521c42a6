"""Tests for the address-coverage-gaps manifest (``/api/coverage-gaps``)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db  # noqa: E402
from app.main import app, get_conn  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _no_match_save(client, query: str) -> dict:
    r = client.post("/api/match", json={"query": query, "save": True})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "no_match", data
    return data


def _seed_no_match_log(query: str) -> int:
    """Persist a no_match log directly (with no candidates), as if an older
    version of the standard table had rejected the query."""
    conn = get_conn()
    log_id = db.insert_match_log(
        conn,
        query=query,
        status="no_match",
        score=None,
        candidate_id=None,
        token_score=None,
        house_score=None,
        matched_tokens=[],
        conflicts=[],
        candidates=[],
    )
    conn.commit()
    return log_id


def _gap_for_house(data: dict, house: str) -> dict:
    matches = [g for g in data["groups"] if g["house"] == house]
    assert len(matches) == 1, [g["house"] for g in data["groups"]]
    return matches[0]


def test_gaps_grouped_by_norm_key_with_count_and_examples(client):
    # Two spellings + a marker variant normalise to the same
    # street/locality/house key -> one gap with occurrence count 3.
    _no_match_save(client, "77 Zxqwlp Road, Kvbrton 99999")
    _no_match_save(client, "77 ZXQWLP ROAD, KVBRTON 99999")
    _no_match_save(client, "No. 77 Zxqwlp Rd, Kvbrton 99999")

    r = client.get("/api/coverage-gaps")
    assert r.status_code == 200
    data = r.json()
    g = _gap_for_house(data, "77")
    assert g["coverage"] == "open"
    assert g["count"] == 3
    assert g["street_tokens"] == ["rd", "zxqwlp"]
    assert g["locality_tokens"] == ["99999", "kvbrton"]
    assert g["representative_query"] == "77 Zxqwlp Road, Kvbrton 99999"
    assert {e["query"] for e in g["examples"]} == {
        "77 Zxqwlp Road, Kvbrton 99999",
        "77 ZXQWLP ROAD, KVBRTON 99999",
        "No. 77 Zxqwlp Rd, Kvbrton 99999",
    }
    assert g["last_seen_at"] >= g["first_seen_at"]
    # the re-run verdict is attached and currently still a no-match (a
    # no_match verdict may still carry a below-threshold best candidate)
    assert g["rerun"]["status"] == "no_match"
    assert data["open"] >= 1
    assert data["source_records"] >= 3


def test_different_house_numbers_never_merge(client):
    _no_match_save(client, "78 Zxqwlp Road, Kvbrton 99999")
    _no_match_save(client, "79 Zxqwlp Road, Kvbrton 99999")
    data = client.get("/api/coverage-gaps").json()
    houses = sorted(g["house"] for g in data["groups"] if g["house"] in {"78", "79"})
    assert houses == ["78", "79"]
    for house in ("78", "79"):
        g = _gap_for_house(data, house)
        assert g["count"] == 1


def test_uploading_address_flips_group_to_covered_without_rewriting_logs(client):
    _no_match_save(client, "76 Qzznox Mews, Vkrton 88007")
    before = client.get("/api/coverage-gaps").json()
    g_before = _gap_for_house(before, "76")
    assert g_before["coverage"] == "open"
    source_log_id = g_before["representative_match_id"]

    # Before the upload: the representative must not auto-match.
    assert g_before["rerun"]["status"] == "no_match"
    log_count_before = client.get("/api/matches?limit=500").json()["total"]

    # Upload the missing standard address: the next manifest open re-runs
    # the representative against the live table and marks it covered.
    csv_bytes = b"id,address,city,zip\nGAP-076,76 Qzznox Mews,Vkrton,88007\n"
    up = client.post("/api/addresses/upload",
                     files={"file": ("g.csv", csv_bytes, "text/csv")})
    assert up.status_code == 200
    assert up.json()["inserted"] == 1

    after = client.get("/api/coverage-gaps").json()
    g_after = _gap_for_house(after, "76")
    assert g_after["coverage"] == "covered"
    assert g_after["group_key"] == g_before["group_key"]
    assert g_after["count"] == 1
    rerun = g_after["rerun"]
    assert rerun["status"] == "auto_matched"
    assert rerun["best_id"] is not None
    assert "Qzznox Mews" in rerun["candidates"][0]["address"]["full_text"]

    # The old no_match record is untouched and no new log was written by
    # opening the manifest.
    detail = client.get(f"/api/matches/{source_log_id}").json()
    assert detail["status"] == "no_match"
    assert client.get("/api/matches?limit=500").json()["total"] == log_count_before


def test_wrong_house_upload_keeps_group_open_with_preferred_candidate(client):
    # An old rejection for house 71; the standard table gains the same
    # street but with a different house number (76). Full street tokens make
    # 76 the preferred candidate, yet the house conflict keeps it below the
    # auto threshold -> the gap must remain 待补录 (open), not covered.
    log_id = _seed_no_match_log("71 Qvkrpl Mews, Zxbrton 88009")
    csv_bytes = b"id,address,city,zip\nGAP-071W,76 Qvkrpl Mews,Zxbrton,88009\n"
    up = client.post("/api/addresses/upload",
                     files={"file": ("w.csv", csv_bytes, "text/csv")})
    assert up.status_code == 200
    assert up.json()["inserted"] == 1

    data = client.get("/api/coverage-gaps").json()
    g = _gap_for_house(data, "71")
    assert g["coverage"] == "open"
    assert g["representative_match_id"] == log_id
    rerun = g["rerun"]
    assert rerun["status"] == "needs_review"
    assert rerun["best_id"] is not None
    assert "Qvkrpl Mews" in rerun["candidates"][0]["address"]["full_text"]
    conflict_fields = {c["field"] for c in rerun["candidates"][0]["conflicts"]}
    assert "house_number" in conflict_fields


def test_status_filter(client):
    covered = client.get("/api/coverage-gaps?status=covered").json()
    open_ = client.get("/api/coverage-gaps?status=open").json()
    assert all(g["coverage"] == "covered" for g in covered["groups"])
    assert all(g["coverage"] == "open" for g in open_["groups"])
    assert any(g["house"] == "76" for g in covered["groups"])
    assert {g["house"] for g in open_["groups"]} >= {"71", "77", "78", "79"}
    assert "76" not in {g["house"] for g in open_["groups"]}
    # summary counts are independent of the filter
    assert covered["total_groups"] == open_["total_groups"]
    assert covered["open"] == open_["open"]
    assert covered["source_records"] == open_["source_records"]


def test_empty_manifest_on_fresh_database():
    # A throwaway database with only the sample addresses (and no no_match
    # logs at all) yields an empty manifest.
    import os
    import tempfile
    from pathlib import Path

    from app import coverage_gaps
    from app.config import get_settings
    from app import ingest as ingest_mod

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
        out = coverage_gaps.compute_gaps(conn, get_settings())
        assert out["groups"] == []
        assert out["total_groups"] == 0
        assert out["open"] == 0 and out["covered"] == 0
        assert out["source_records"] == 0
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


def test_invalid_status_filter(client):
    r = client.get("/api/coverage-gaps", params={"status": "bogus"})
    assert r.status_code == 400
