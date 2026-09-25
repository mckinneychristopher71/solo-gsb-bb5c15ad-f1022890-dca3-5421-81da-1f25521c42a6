"""Tests for the rule A/B comparison experiment feature."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest  # noqa: E402
from app.main import app, get_conn, get_settings  # noqa: E402
from app import experiments  # noqa: E402


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


def _profile(name="A", **overrides):
    base = {
        "name": name,
        "auto_threshold": 0.8,
        "reject_threshold": 0.35,
        "margin_threshold": 0.06,
        "token_weight": 0.7,
    }
    base.update(overrides)
    return base


def _body(**overrides):
    base = {
        "profiles": [_profile("基线A"), _profile("激进B", auto_threshold=0.7)],
        "queries": ["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas"],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- happy path


def test_experiment_runs_each_profile_on_same_snapshot(client):
    r = client.post("/api/experiments", json=_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"].startswith("EXP-")
    assert data["address_count"] >= 15
    assert data["table_snapshot"]["count"] == data["address_count"]
    assert data["summary"]["query_count"] == 2

    # First query: 0.7864 score needs review at auto=0.8 but auto-matches at 0.7.
    q0 = data["results"][0]
    assert q0["query"] == "1010 Lake Dr, Austin TX"
    assert q0["disagreement"]["conclusion_differs"] is True
    assert q0["disagreement"]["status_agree"] is False
    assert q0["disagreement"]["best_id_agree"] is True
    statuses = [pr["status"] for pr in q0["profiles"]]
    assert statuses == ["needs_review", "auto_matched"]
    # both profiles still prefer the same address
    assert len({pr["best_id"] for pr in q0["profiles"]}) == 1

    # decisive threshold annotation: the auto cut-off fired for A, nothing for B
    a_thr, b_thr = (pr["thresholds"] for pr in q0["profiles"])
    assert a_thr["decisive"] == "auto_threshold"
    assert a_thr["auto_threshold"]["triggered"] is True
    assert a_thr["reject_threshold"]["triggered"] is False
    assert b_thr["decisive"] is None
    # margin measured against the runner-up
    assert a_thr["margin_threshold"]["actual"] >= 0


def test_margin_threshold_marks_ambiguity(client):
    # Ambiguous query (50 vs 52 Hillcrest, no house number): margin fires
    # under both profiles.
    r = client.post("/api/experiments", json=_body(
        queries=["Hillcrest Road, Dallas"],
    ))
    q0 = r.json()["results"][0]
    for pr in q0["profiles"]:
        assert pr["status"] == "needs_review"
        assert pr["thresholds"]["margin_threshold"]["triggered"] is True
        assert pr["thresholds"]["decisive"] == "margin_threshold"
    assert q0["disagreement"]["conclusion_differs"] is False


def test_reject_threshold_and_no_candidates(client):
    r = client.post("/api/experiments", json=_body(
        queries=["1 Milky Way, Mars Base"],
    ))
    q0 = r.json()["results"][0]
    for pr in q0["profiles"]:
        assert pr["status"] == "no_match"
        # the matcher still reports a (zero-scored) best, but it is rejected
        assert pr["best_score"] is not None
        assert pr["thresholds"]["decisive"] == "reject_threshold"
        assert pr["thresholds"]["reject_threshold"]["triggered"] is True
    assert q0["disagreement"]["conclusion_differs"] is False


def test_token_weight_changes_scores(client):
    # Same thresholds, different weights -> different best_score, raw matcher.
    r = client.post("/api/experiments", json={
        "profiles": [_profile("词元0.9", token_weight=0.9),
                     _profile("词元0.1", token_weight=0.1)],
        "queries": ["1010 Lake Dr, Austin TX"],
    })
    q0 = r.json()["results"][0]
    s0, s1 = (pr["best_score"] for pr in q0["profiles"])
    assert s0 != s1
    # score deltas are reported relative to the first profile
    assert q0["profiles"][0]["score_delta_vs_first"] == 0.0
    assert q0["profiles"][1]["score_delta_vs_first"] == round(s1 - s0, 4)


# ----------------------------------------------------------- candidate diff


def test_candidate_diff_union_ranking(client):
    r = client.post("/api/experiments", json=_body(
        profiles=[_profile("A"), _profile("B", token_weight=0.95)],
        queries=["Lake Dr, Austin TX"],
    ))
    q0 = r.json()["results"][0]
    diff = q0["candidate_diff"]
    # every address appearing in either top-5 list is in the union
    expected = set()
    for pr in q0["profiles"]:
        expected.update(c["address"]["id"] for c in pr["candidates"])
    assert {row["address_id"] for row in diff["ranking"]} == expected
    for row in diff["ranking"]:
        names = {cell["profile"] for cell in row["cells"] if cell["match"]}
        assert names <= {"基线A", "激进B"} or names <= {"A", "B"}
        for cell in row["cells"]:
            if cell["match"]:
                assert cell["match"]["rank"] >= 1
                assert isinstance(cell["match"]["score"], (int, float))


# ----------------------------------------------------- itemized validation


def _post_errors(client, body):
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 400, r.text
    return r.json()["errors"]


def test_invalid_profiles_and_duplicate_query_reported_per_item(client):
    errors = _post_errors(client, {
        "profiles": [
            {"name": "X", "auto_threshold": 1.5, "reject_threshold": 0.3,
             "margin_threshold": 0.1, "token_weight": 0.7},
            {"name": "X", "auto_threshold": 0.4, "reject_threshold": 0.5,
             "margin_threshold": -1, "token_weight": 2},
        ],
        "queries": ["abc", "abc", "   ", 123],
    })
    # all problems are returned together, pinned to their index/field
    assert {"index": 0, "field": "auto_threshold"} in \
        [{"index": e["index"], "field": e["field"]} for e in errors
         if e["type"] == "profile"]
    assert any(e["type"] == "profile" and e["index"] == 1 and
               e["field"] == "name" and "重复" in e["message"] for e in errors)
    assert any(e["type"] == "profile" and e["index"] == 1 and
               e["field"] == "reject_threshold" for e in errors)
    assert any(e["type"] == "profile" and e["index"] == 1 and
               e["field"] == "margin_threshold" for e in errors)
    assert any(e["type"] == "profile" and e["index"] == 1 and
               e["field"] == "token_weight" for e in errors)
    assert any(e["type"] == "query" and e["index"] == 1 and
               "重复" in e["message"] for e in errors)
    assert any(e["type"] == "query" and e["index"] == 2 for e in errors)
    assert any(e["type"] == "query" and e["index"] == 3 for e in errors)

    # nothing was executed: the history total is unchanged
    before = client.get("/api/experiments").json()["total"]
    client.post("/api/experiments", json={
        "profiles": [_profile("A"), _profile("B")], "queries": ["dup", "dup"],
    })
    assert client.get("/api/experiments").json()["total"] == before


def test_profile_and_query_count_limits(client):
    errors = _post_errors(client, {
        "profiles": [_profile(f"P{i}") for i in range(5)],
        "queries": ["q"],
    })
    assert any(e["type"] == "profiles" and "2~4" in e["message"] for e in errors)

    errors = _post_errors(client, {
        "profiles": [_profile("A"), _profile("B")],
        "queries": [f"q{i}" for i in range(101)],
    })
    assert any(e["type"] == "queries" and "1~100" in e["message"] for e in errors)

    errors = _post_errors(client, {
        "profiles": [_profile("only")],
        "queries": [],
    })
    kinds = {(e["type"],) for e in errors}
    assert ("profiles",) in kinds
    assert ("queries",) in kinds


def test_non_numeric_and_nan_rejected(client):
    # NaN is sent as a raw JSON token (Python's json emits it; the server-side
    # parser accepts the token but the validator rejects non-finite numbers).
    raw = (
        '{"profiles": ['
        '{"name": "A", "auto_threshold": true, "reject_threshold": 0.3,'
        ' "margin_threshold": 0.1, "token_weight": NaN},'
        '{"name": "B", "auto_threshold": 0.8, "reject_threshold": 0.2,'
        ' "margin_threshold": 0.1, "token_weight": 0.7}],'
        ' "queries": ["ok"]}'
    )
    r = client.post("/api/experiments", content=raw,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    errors = r.json()["errors"]
    fields = {e["field"] for e in errors if e["type"] == "profile" and e["index"] == 0}
    assert "auto_threshold" in fields
    assert "token_weight" in fields


# -------------------------------------------------------------- persistence


def test_experiment_persisted_and_replayable_by_id(client):
    run = client.post("/api/experiments", json=_body()).json()
    fetched = client.get(f"/api/experiments/{run['id']}").json()
    for key in ("profiles", "queries", "results", "summary",
                "address_count", "table_snapshot"):
        assert fetched[key] == run[key], key

    listing = client.get("/api/experiments").json()
    assert listing["total"] >= 1
    first = listing["items"][0]
    assert first["id"] == run["id"]
    assert first["query_count"] == 2
    assert [p["name"] for p in first["profiles"]] == ["基线A", "激进B"]
    # summary rows carry per-profile status counts
    assert set(first["summary"]["profiles"][0]) == {
        "name", "auto_matched", "needs_review", "no_match"}

    assert client.get("/api/experiments/EXP-does-not-exist").status_code == 404


def test_same_input_produces_stable_results(client):
    body = _body(queries=["1010 Lake Dr, Austin TX", "123 Main St ny",
                          "Hillcrest Road, Dallas"])
    first = client.post("/api/experiments", json=body).json()
    second = client.post("/api/experiments", json=body).json()
    for key in ("results", "summary", "profiles", "queries",
                "address_count", "table_snapshot"):
        assert first[key] == second[key], key


def test_experiment_uses_frozen_snapshot(client):
    body = _body(queries=["304 Lake Dr, Austin TX"])
    first = client.post("/api/experiments", json=body).json()

    # mutate the address table, then re-run: snapshot digest must differ but
    # the persisted first experiment still replays with its old snapshot.
    client.post("/api/addresses", json={"addresses": [
        {"address": "305 Lake Drive, Austin, TX 73301", "external_id": "EXP-305"},
    ]})
    second = client.post("/api/experiments", json=body).json()
    assert first["table_snapshot"] != second["table_snapshot"]

    replayed = client.get(f"/api/experiments/{first['id']}").json()
    assert replayed["table_snapshot"] == first["table_snapshot"]
    assert replayed["results"] == first["results"]


def test_experiment_does_not_write_match_logs(client):
    before = client.get("/api/matches", params={"limit": 1}).json()["total"]
    client.post("/api/experiments", json=_body(queries=[
        "1010 Lake Dr, Austin TX", "1 Milky Way, Mars Base",
    ]))
    after = client.get("/api/matches", params={"limit": 1}).json()["total"]
    assert after == before


def test_experiment_result_candidates_have_full_detail(client):
    data = client.post("/api/experiments", json=_body(
        queries=["1010 Lake Dr, Austin TX"])).json()
    cand = data["results"][0]["profiles"][0]["candidates"][0]
    assert cand["address"]["id"]
    assert cand["score"] is not None
    assert "street_tokens" in cand and "matched_tokens" in cand
    assert "conflicts" in cand


# ----------------------------------------------------------- unit validation


def test_validate_payload_helpers():
    profiles, queries, errors = experiments.validate_payload({
        "profiles": [_profile("A"), _profile(" A ")],
        "queries": ["  hello ", "hello"],
    })
    # "  hello " is stripped to "hello" -> duplicate; " A " strips to "A" too
    assert queries == ["hello", "hello"]
    assert any(e["type"] == "query" and e["index"] == 1 for e in errors)
    assert any(e["type"] == "profile" and e["field"] == "name" for e in errors)

    _, _, errors = experiments.validate_payload("not-an-object")
    assert errors and errors[0]["type"] == "request"
