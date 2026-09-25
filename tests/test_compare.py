"""Tests for the historical regression comparison of persisted experiments."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, ingest  # noqa: E402
from app.main import app, get_conn, get_settings  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_tables(client):
    """Reset the shared test DB so other modules' uploads/logs don't leak in."""
    conn = get_conn()
    conn.execute("DELETE FROM ab_experiments")
    conn.execute("DELETE FROM match_logs")
    conn.execute("DELETE FROM addresses")
    conn.commit()
    sample = Path(get_settings().sample_csv)
    ingest.add_many(conn, ingest.parse_csv(sample.read_bytes()))
    yield
    conn.execute("DELETE FROM ab_experiments")
    conn.commit()


def _profile(name, **overrides):
    base = {
        "name": name,
        "auto_threshold": 0.8,
        "reject_threshold": 0.35,
        "margin_threshold": 0.06,
        "token_weight": 0.7,
    }
    base.update(overrides)
    return base


QUERIES = [
    "1010 Lake Dr, Austin TX",
    "Hillcrest Road, Dallas",
    "123 Main Street, New York, NY 10001",
]

# Base run: P1/P2 strict auto cut-off, P3 lenient.  Target run: P1 loosened,
# P2 same thresholds but a different token weight, P3 tightened.
BASE_PROFILES = [
    _profile("P1"),
    _profile("P2"),
    _profile("P3", auto_threshold=0.7),
]
TARGET_PROFILES = [
    _profile("P1", auto_threshold=0.7),
    _profile("P2", token_weight=0.9),
    _profile("P3"),
]


def _run_pair(client, base_profiles=BASE_PROFILES, target_profiles=TARGET_PROFILES,
              base_queries=QUERIES, target_queries=QUERIES):
    base = client.post("/api/experiments", json={
        "profiles": base_profiles, "queries": base_queries}).json()
    target = client.post("/api/experiments", json={
        "profiles": target_profiles, "queries": target_queries}).json()
    return base, target


def _compare(client, base_id, target_id):
    return client.post("/api/experiments/compare", json={
        "base_id": base_id, "target_id": target_id})


def _cell(data, query, profile):
    for cell in data["cells"]:
        if cell["query"] == query and cell["profile"] == profile:
            return cell
    raise AssertionError(f"cell not found: {query} / {profile}")


def _insert_experiment(exp_id, profiles, queries, results=None, digest="d0"):
    """Hand-craft a persisted experiment row (bypasses the runner)."""
    if results is None:
        results = [
            {"index": i, "query": q,
             "profiles": [{"profile": name, "index": pi, "status": "auto_matched",
                           "best_id": 1, "best_score": 0.9}
                          for pi, name in enumerate(profiles)]}
            for i, q in enumerate(queries)
        ]
    conn = get_conn()
    db.insert_ab_experiment(
        conn,
        experiment_id=exp_id,
        profiles=[{"name": n} for n in profiles],
        queries=queries,
        table_snapshot=json.dumps({"count": 1, "max_id": 1, "digest": digest}),
        address_count=1,
        results=results,
        summary={},
        created_at="2026-09-24T00:00:00+00:00",
    )
    conn.commit()


# ------------------------------------------------------------- happy path


def test_compare_classifies_every_cell(client):
    base, target = _run_pair(client)
    r = _compare(client, base["id"], target["id"])
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["base"]["id"] == base["id"]
    assert data["target"]["id"] == target["id"]
    assert data["aligned_profiles"] == ["P1", "P2", "P3"]
    assert data["queries"]["common"] == 3
    assert data["queries"]["only_in_base"] == []
    assert data["queries"]["only_in_target"] == []
    # same address table -> snapshots comparable
    assert data["snapshot"]["comparable"] is True
    assert data["snapshot"]["note"] is None

    q1, q2, q3 = QUERIES
    # status moved up: needs_review -> auto_matched
    assert _cell(data, q1, "P1")["category"] == "improved"
    # same status/address, only the score moved (token weight changed)
    assert _cell(data, q1, "P2")["category"] == "score_only"
    assert _cell(data, q2, "P2")["category"] == "score_only"
    # status moved down: auto_matched -> needs_review
    assert _cell(data, q1, "P3")["category"] == "regressed"
    # identical runs
    for profile in ("P1", "P2", "P3"):
        assert _cell(data, q3, profile)["category"] == "unchanged"
    assert _cell(data, q2, "P1")["category"] == "unchanged"
    assert _cell(data, q2, "P3")["category"] == "unchanged"

    assert data["summary"] == {
        "total_cells": 9,
        "improved": 1,
        "regressed": 1,
        "score_only": 2,
        "unchanged": 5,
    }

    cell = _cell(data, q1, "P2")
    assert cell["base"]["status"] == "needs_review"
    assert cell["base"]["best_score"] == 0.7864
    # address ids are not stable across test resets; both sides must agree
    assert cell["base"]["best_id"] == cell["target"]["best_id"]
    assert cell["target"]["best_score"] == 0.9288
    assert cell["score_delta"] == round(0.9288 - 0.7864, 4)
    assert cell["category_label"] == "仅分数变化"
    assert cell["reason"]


def test_compare_same_status_best_address_flip_is_regression(client):
    _insert_experiment(
        "EXP-base-flip", ["A"], ["q1"],
        results=[{"index": 0, "query": "q1", "profiles": [
            {"profile": "A", "index": 0, "status": "auto_matched",
             "best_id": 1, "best_score": 0.9}]}],
    )
    _insert_experiment(
        "EXP-target-flip", ["A"], ["q1"],
        results=[{"index": 0, "query": "q1", "profiles": [
            {"profile": "A", "index": 0, "status": "auto_matched",
             "best_id": 2, "best_score": 0.9}]}],
    )
    data = _compare(client, "EXP-base-flip", "EXP-target-flip").json()
    cell = data["cells"][0]
    assert cell["category"] == "regressed"
    assert "首选地址变化" in cell["reason"]
    assert data["summary"]["regressed"] == 1


def test_compare_is_read_only(client):
    base, target = _run_pair(client)
    before = client.get("/api/experiments").json()["total"]
    r = _compare(client, base["id"], target["id"])
    assert r.status_code == 200
    assert client.get("/api/experiments").json()["total"] == before


# ---------------------------------------------------- alignment validation


def test_compare_unknown_experiment_is_404(client):
    base, _ = _run_pair(client)
    assert _compare(client, base["id"], "EXP-missing").status_code == 404
    assert _compare(client, "EXP-missing", base["id"]).status_code == 404
    assert _compare(client, "EXP-missing", "EXP-missing-2").status_code == 404


def test_compare_same_experiment_rejected(client):
    base, _ = _run_pair(client)
    r = _compare(client, base["id"], base["id"])
    assert r.status_code == 400
    assert any("相同" in e["message"] for e in r.json()["errors"])


def test_compare_request_shape_validated(client):
    r = client.post("/api/experiments/compare", json={})
    assert r.status_code == 400
    types = {e["type"] for e in r.json()["errors"]}
    assert types == {"base_id", "target_id"}

    r = client.post("/api/experiments/compare",
                    content='["not-an-object"]',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["errors"][0]["type"] == "request"


def test_compare_missing_profiles_rejected(client):
    base, target = _run_pair(
        client,
        base_profiles=[_profile("P1"), _profile("P2")],
        target_profiles=[_profile("P1"), _profile("P3")],
    )
    r = _compare(client, base["id"], target["id"])
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert any(e["type"] == "profiles" and "P2" in e["message"] and "缺少" in e["message"]
               for e in errors)
    assert any(e["type"] == "profiles" and "P3" in e["message"] and "缺少" in e["message"]
               for e in errors)


def test_compare_duplicate_queries_rejected(client):
    _insert_experiment("EXP-base-dup", ["A"], ["q1", " q1  "])
    _insert_experiment("EXP-target-ok", ["A"], ["q1"])
    r = _compare(client, "EXP-base-dup", "EXP-target-ok")
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert any(e["type"] == "queries" and "重复" in e["message"]
               and e.get("side") == "base" for e in errors)


def test_compare_duplicate_profile_names_rejected(client):
    _insert_experiment("EXP-base-pdup", ["A", "A"], ["q1"])
    _insert_experiment("EXP-target-pok", ["A"], ["q1"])
    r = _compare(client, "EXP-base-pdup", "EXP-target-pok")
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert any(e["type"] == "profiles" and "重复" in e["message"] for e in errors)


# --------------------------------------------------------- query alignment


def test_compare_aligns_queries_after_stripping(client):
    base, _ = _run_pair(client, base_queries=["1010 Lake Dr, Austin TX"])
    # A persisted row whose query carries surrounding whitespace still aligns.
    _insert_experiment(
        "EXP-target-padded", ["P1", "P2", "P3"], ["  1010 Lake Dr, Austin TX  "],
        results=[{"index": 0, "query": "  1010 Lake Dr, Austin TX  ",
                  "profiles": [
                      {"profile": n, "index": i, "status": "no_match",
                       "best_id": None, "best_score": 0.1}
                      for i, n in enumerate(["P1", "P2", "P3"])]}],
    )
    r = _compare(client, base["id"], "EXP-target-padded")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queries"]["common"] == 1
    assert len(data["cells"]) == 3
    # the canonical (stripped) query text is reported
    assert data["cells"][0]["query"] == "1010 Lake Dr, Austin TX"


def test_compare_unpaired_queries_excluded_from_counts(client):
    base, target = _run_pair(
        client,
        base_queries=["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas"],
        target_queries=["1010 Lake Dr, Austin TX", "1 Milky Way, Mars Base"],
    )
    r = _compare(client, base["id"], target["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queries"]["common"] == 1
    assert data["queries"]["only_in_base"] == ["Hillcrest Road, Dallas"]
    assert data["queries"]["only_in_target"] == ["1 Milky Way, Mars Base"]
    # 1 common query x 3 aligned profiles
    assert data["summary"]["total_cells"] == 3
    assert sum(data["summary"][k] for k in
               ("improved", "regressed", "score_only", "unchanged")) == 3


# ------------------------------------------------------- snapshot handling


def test_compare_flags_snapshot_digest_mismatch(client):
    base = client.post("/api/experiments", json={
        "profiles": [_profile("P1"), _profile("P2")],
        "queries": ["1010 Lake Dr, Austin TX"]}).json()
    # mutate the address table so the next experiment freezes a new snapshot
    client.post("/api/addresses", json={"addresses": [
        {"address": "999 Nowhere Blvd, Austin, TX 73301", "external_id": "CMP-1"},
    ]})
    target = client.post("/api/experiments", json={
        "profiles": [_profile("P1"), _profile("P2")],
        "queries": ["1010 Lake Dr, Austin TX"]}).json()

    r = _compare(client, base["id"], target["id"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["snapshot"]["comparable"] is False
    assert "digest" in data["snapshot"]["note"]
    assert data["snapshot"]["base_digest"] != data["snapshot"]["target_digest"]
    # per-cell differences are still computed and returned
    assert len(data["cells"]) == 2
    assert data["summary"]["total_cells"] == 2
