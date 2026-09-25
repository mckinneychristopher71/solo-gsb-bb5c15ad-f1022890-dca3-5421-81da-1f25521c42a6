"""Tests for the review verdict replay (``/api/review-replay``)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db  # noqa: E402
from app.main import app, get_conn  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# Match ids created by this module, shared across its ordered tests.
STATE: dict[str, int] = {}


def _match_save(client, query: str) -> dict:
    r = client.post("/api/match", json={"query": query, "save": True})
    assert r.status_code == 200
    return r.json()


def _seed_review_direct(match_id: int, action: str,
                        selected_id: int | None = None) -> None:
    """Write a review verdict straight through the persistence layer.

    The claim+review API only serves *pending* records, so verdicts on
    auto_matched / no_match logs are seeded directly here, mirroring what the
    review endpoint writes (chosen-address snapshot included).
    """
    conn = get_conn()
    row = db.get_match_log(conn, match_id)
    assert row is not None
    score = text = None
    if action in ("confirmed", "overridden"):
        if action == "confirmed":
            selected_id = row["candidate_id"]
            score = row["score"]
        else:
            for cand in json.loads(row["candidates_json"]):
                if cand["address"]["id"] == selected_id:
                    score = cand["score"]
                    break
        addr = db.get_address(conn, selected_id)
        text = addr["full_text"] if addr else None
    db.review_match_log(
        conn, match_id, action=action,
        selected_id=selected_id, score=score, selected_text=text,
    )
    conn.commit()


def _review(client, match_id: int, action: str, selected_id: int | None = None) -> None:
    status = client.get(f"/api/matches/{match_id}").json()["status"]
    if status != "needs_review":
        _seed_review_direct(match_id, action, selected_id)
        return
    # Pending records go through the real claim + token review flow.
    claim = client.post(f"/api/matches/{match_id}/claim", json={"reviewer": "pytest"})
    assert claim.status_code == 200, claim.text
    body: dict = {"action": action, "token": claim.json()["token"]}
    if selected_id is not None:
        body["selected_id"] = selected_id
    r = client.post(f"/api/matches/{match_id}/review", json=body)
    assert r.status_code == 200, r.text


def _replay(client, category: str | None = None) -> dict:
    qs = f"?category={category}" if category else ""
    r = client.get(f"/api/review-replay{qs}")
    assert r.status_code == 200
    return r.json()


def _item_for(data: dict, match_id: int) -> dict:
    matches = [it for it in data["items"] if it["match_id"] == match_id]
    assert len(matches) == 1, [it["match_id"] for it in data["items"]]
    return matches[0]


def test_replay_categories_and_summary(client):
    before = _replay(client)

    # 1) consistent: an auto-matched query the human confirmed -> the replay
    #    auto-hits the very same address.
    m1 = _match_save(client, "123 main st ny")
    assert m1["status"] == "auto_matched"
    _review(client, m1["match_id"], "confirmed")
    STATE["consistent"] = m1["match_id"]

    # 2) conflict: the human overrode the auto-hit "100 Lake Drive" to
    #    "1000 Lake Drive"; the replay still auto-hits the former.
    m2 = _match_save(client, "100 Lake Dr, Austin TX")
    assert m2["status"] == "auto_matched"
    other = next(
        c["address"]["id"] for c in m2["candidates"]
        if "1000 Lake Drive" in c["address"]["full_text"]
    )
    assert other != m2["best_id"]
    _review(client, m2["match_id"], "overridden", other)
    STATE["conflict"] = m2["match_id"]

    # 3) missed: the human confirmed a needs_review record; the replay still
    #    cannot auto-hit the chosen address (house number 1010 vs 1000).
    m3 = _match_save(client, "1010 Lake Dr, Austin TX")
    assert m3["status"] == "needs_review"
    _review(client, m3["match_id"], "confirmed")
    STATE["missed"] = m3["match_id"]

    # 4) consistent (for now): a no_match query the human judged no-match.
    m4 = _match_save(client, "999 Zxqwv Replay Blvd, Nowhereton")
    assert m4["status"] == "no_match"
    _review(client, m4["match_id"], "rejected")
    STATE["rejected"] = m4["match_id"]

    data = _replay(client)

    it1 = _item_for(data, STATE["consistent"])
    assert it1["category"] == "consistent"
    assert it1["category_label"] == "一致"
    assert it1["review_action"] == "confirmed"
    assert it1["review_action_label"] == "确认首选"
    assert it1["selected_id"] == m1["best_id"]
    assert it1["selected_full_text"] == "123 Main Street"
    assert it1["selected_missing"] is False
    assert it1["replay_status"] == "auto_matched"
    assert it1["replay_best_id"] == it1["selected_id"]
    assert it1["replay_best_full_text"] == "123 Main Street"
    assert it1["replay"]["best_id"] == it1["selected_id"]
    assert it1["replay"]["candidates"]

    it2 = _item_for(data, STATE["conflict"])
    assert it2["category"] == "conflict"
    assert it2["category_label"] == "冲突"
    assert it2["review_action"] == "overridden"
    assert it2["review_action_label"] == "改派"
    assert it2["selected_id"] == other
    assert it2["selected_full_text"] == "1000 Lake Drive"
    assert it2["replay_status"] == "auto_matched"
    assert it2["replay_best_id"] == m2["best_id"]
    assert it2["replay_best_id"] != it2["selected_id"]

    it3 = _item_for(data, STATE["missed"])
    assert it3["category"] == "missed"
    assert it3["category_label"] == "漏命中"
    assert it3["replay_status"] == "needs_review"
    assert it3["replay_best_id"] == it3["selected_id"]  # preferred but not auto

    it4 = _item_for(data, STATE["rejected"])
    assert it4["category"] == "consistent"
    assert it4["review_action"] == "rejected"
    assert it4["review_action_label"] == "判无匹配"
    assert it4["selected_id"] is None
    assert it4["replay_status"] == "no_match"

    # summary counts: exactly four new reviewed records on top of `before`
    assert data["total_reviewed"] == before["total_reviewed"] + 4
    assert data["consistent"] == before["consistent"] + 2
    assert data["conflict"] == before["conflict"] + 1
    assert data["missed"] == before["missed"] + 1
    assert data["false_hit"] == before["false_hit"]
    assert data["risk_count"] == (
        data["conflict"] + data["missed"] + data["false_hit"]
    )
    assert data["total_reviewed"] == (
        data["consistent"] + data["conflict"] + data["missed"] + data["false_hit"]
    )
    assert data["thresholds"]["auto_threshold"] == pytest.approx(0.80)

    # risks first (conflict < false_hit < missed < consistent), then match id
    rank = {"conflict": 0, "false_hit": 1, "missed": 2, "consistent": 3}
    keys = [(rank[it["category"]], it["match_id"]) for it in data["items"]]
    assert keys == sorted(keys)

    # every item carries the original verdict, the current best and a reason
    for it in data["items"]:
        assert it["verdict_reason"]
        assert "replay_best_score" in it
        assert it["replay"]["query"] == it["query"]


def test_upload_flips_rejected_record_to_false_hit(client):
    # The rejected query from the previous test becomes auto-matchable once
    # the missing address is uploaded -> 误命中 (false_hit).
    before = _replay(client)
    csv_bytes = b"id,address,city,zip\nRPL-999,999 Zxqwv Replay Blvd,Nowhereton,99999\n"
    up = client.post("/api/addresses/upload",
                     files={"file": ("r.csv", csv_bytes, "text/csv")})
    assert up.status_code == 200
    assert up.json()["inserted"] == 1

    data = _replay(client)
    item = _item_for(data, STATE["rejected"])
    assert item["category"] == "false_hit"
    assert item["category_label"] == "误命中"
    assert item["replay_status"] == "auto_matched"
    assert "Zxqwv Replay" in item["replay_best_full_text"]
    assert data["false_hit"] == before["false_hit"] + 1
    assert data["consistent"] == before["consistent"] - 1
    assert data["total_reviewed"] == before["total_reviewed"]


def test_category_filter_keeps_summary_counts(client):
    full = _replay(client)
    assert full["total_reviewed"] >= 4
    for cat in ("consistent", "conflict", "missed", "false_hit"):
        filtered = _replay(client, cat)
        assert all(it["category"] == cat for it in filtered["items"])
        assert len(filtered["items"]) == full[cat]
        # summary counts are independent of the filter
        assert filtered["total_reviewed"] == full["total_reviewed"]
        assert filtered[cat] == full[cat]
        assert filtered["risk_count"] == full["risk_count"]
    # every category built by this module is represented
    for cat in ("consistent", "conflict", "missed", "false_hit"):
        assert full[cat] >= 1


def test_invalid_category_filter(client):
    r = client.get("/api/review-replay", params={"category": "bogus"})
    assert r.status_code == 400


def test_selected_address_deleted_keeps_snapshot(client):
    # Confirm an auto-hit, then delete the chosen address directly: the FK
    # nulls selected_id, but the durable review-time snapshot keeps the
    # original ID/full text; the replay marks the address missing yet still
    # classifies by the stored choice (here: no auto-hit -> missed).
    r = client.post("/api/addresses", json={
        "addresses": [{"address": "555 Zxqwv Replaydel Avenue, Delton 90001"}],
        "skip_duplicates": True,
    })
    assert r.json()["inserted"] == 1
    addr = next(
        a for a in client.get("/api/addresses?limit=500").json()["items"]
        if "Replaydel" in a["full_text"]
    )
    m = _match_save(client, "555 Zxqwv Replaydel Ave, Delton 90001")
    assert m["status"] == "auto_matched"
    _review(client, m["match_id"], "confirmed")

    # The snapshot columns are populated at review write time.
    record = client.get(f"/api/matches/{m['match_id']}").json()
    assert record["selected_address_id"] == addr["id"]
    assert record["selected_address_text"] == "555 Zxqwv Replaydel Avenue, Delton 90001"

    conn = get_conn()
    conn.execute("DELETE FROM addresses WHERE id = ?", (addr["id"],))
    conn.commit()

    data = _replay(client)
    item = _item_for(data, m["match_id"])
    assert item["selected_missing"] is True
    assert item["selected_known"] is True
    # The replay restores the original ID + full text + review score from the
    # snapshot; the nulled FK no longer erases the human verdict.
    assert item["selected_id"] == addr["id"]
    assert item["selected_full_text"] == "555 Zxqwv Replaydel Avenue, Delton 90001"
    assert item["review_score"] == m["best_score"]
    assert "已删除" in item["verdict_reason"]
    # The deleted exact address cannot be re-hit on the current table -> missed.
    assert item["category"] == "missed"


def test_deleted_snapshot_address_conflict_when_replay_hits_other(client):
    # An overridden choice whose address is later deleted: if the replay now
    # auto-hits a *different* live address, the stored snapshot still drives a
    # 冲突 (conflict) verdict naming the original deleted choice.
    m = _match_save(client, "100 Lake Dr, Austin TX")
    assert m["status"] == "auto_matched"
    other = next(
        c["address"]["id"] for c in m["candidates"]
        if "1000 Lake Drive" in c["address"]["full_text"]
    )
    _review(client, m["match_id"], "overridden", other)

    conn = get_conn()
    conn.execute("DELETE FROM addresses WHERE id = ?", (other,))
    conn.commit()

    item = _item_for(_replay(client), m["match_id"])
    assert item["selected_id"] == other
    assert item["selected_known"] is True
    assert item["selected_missing"] is True
    assert item["selected_full_text"] == "1000 Lake Drive"
    assert item["category"] == "conflict"
    assert f"#{other}" in item["verdict_reason"]
    assert "已删除" in item["verdict_reason"]


def test_legacy_record_with_lost_selected_id_is_unknown(client):
    # Simulate a legacy reviewed record written before review-time snapshots
    # existed, whose chosen address was already deleted (FK nulled): the
    # original ID cannot be determined and must surface as 原选择未知 without
    # guessing from the candidates.
    conn = get_conn()
    m = _match_save(client, "200 Legacy Snap Blvd, Oldtown 80001")
    # Force the pre-snapshot state directly: reviewed, positive action, but no
    # durable snapshot and a nulled FK.
    with conn:
        conn.execute(
            """
            UPDATE match_logs
               SET status = 'reviewed', review_action = 'confirmed',
                   selected_id = NULL, selected_address_id = NULL,
                   selected_address_text = NULL, score = 0.61,
                   reviewed_at = ?
             WHERE id = ?
            """,
            ("2020-01-01T00:00:00+00:00", m["match_id"]),
        )

    item = _item_for(_replay(client), m["match_id"])
    assert item["category"] == "unknown"
    assert item["category_label"] == "原选择未知"
    assert item["selected_known"] is False
    assert item["selected_id"] is None
    assert item["selected_full_text"] is None
    assert item["selected_missing"] is False
    assert "无法确定" in item["verdict_reason"]
    assert _replay(client)["unknown"] >= 1


def test_unknown_category_filter_and_summary(client):
    full = _replay(client)
    assert "unknown" in full
    assert full["risk_count"] == (
        full["conflict"] + full["missed"] + full["false_hit"] + full["unknown"]
    )
    assert full["total_reviewed"] == (
        full["consistent"] + full["conflict"] + full["missed"]
        + full["false_hit"] + full["unknown"]
    )
    filtered = _replay(client, "unknown")
    assert all(it["category"] == "unknown" for it in filtered["items"])
    assert len(filtered["items"]) == full["unknown"]
    # summary counts stay independent of the filter
    assert filtered["total_reviewed"] == full["total_reviewed"]


def test_old_database_backfills_snapshot_for_live_selection():
    # init_db backfills the durable snapshot for legacy reviews whose chosen
    # address still exists, so future deletions no longer lose the verdict.
    import os
    import tempfile
    from pathlib import Path

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
        ingest_mod.add_many(
            conn, ingest_mod.parse_csv(Path(get_settings().sample_csv).read_bytes())
        )
        # Insert a "legacy" reviewed log pointing at a live address without any
        # snapshot columns populated.
        addr_id = conn.execute("SELECT MIN(id) FROM addresses").fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO match_logs
                (query, status, score, selected_id, selected_address_id,
                 selected_address_text, candidates_json, created_at,
                 reviewed_at, review_action)
            VALUES (?, 'reviewed', 0.72, ?, NULL, NULL, '[]', ?, ?, 'confirmed')
            """,
            ("legacy query", addr_id, "2020-01-01T00:00:00+00:00",
             "2020-01-01T00:00:00+00:00"),
        )
        log_id = cur.lastrowid
        conn.commit()
        conn.close()
        conn = None

        # Re-open: init_db runs the migration/backfill.
        conn = db.connect()
        db.init_db(conn)
        row = conn.execute(
            "SELECT selected_address_id, selected_address_text FROM match_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        assert row["selected_address_id"] == addr_id
        assert row["selected_address_text"]
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


def test_group_review_snapshot_survives_address_deletion(client):
    # Upload one address; two same-case queries with a different house number
    # become needs_review and share a group. A group confirmation snapshots the
    # chosen address; deleting the address afterwards must keep every member's
    # original ID/full text in the replay (group-review write path included).
    up = client.post("/api/addresses", json={
        "addresses": [{"address": "700 Zxqwv Group Snap Road, Groupton 70001"}],
        "skip_duplicates": True,
    })
    assert up.json()["inserted"] == 1
    addr = next(
        a for a in client.get("/api/addresses?limit=500").json()["items"]
        if "Group Snap" in a["full_text"]
    )

    q1 = "701 Zxqwv Group Snap Road, Groupton 70001"
    q2 = "No. 701 Zxqwv Group Snap Rd, Groupton 70001"
    ids = []
    for q in (q1, q2):
        m = _match_save(client, q)
        assert m["status"] == "needs_review", m["status"]
        ids.append(m["match_id"])

    groups = client.get("/api/review-groups").json()["groups"]
    group = next(
        g for g in groups if set(ids).issubset(set(g["match_ids"]))
    )
    assert group["candidates"][0]["address"]["id"] == addr["id"]
    target_text = group["candidates"][0]["address"]["full_text"]
    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"],
        "match_ids": group["match_ids"],
        "action": "confirmed",
    })
    assert r.status_code == 200, r.text

    conn = get_conn()
    conn.execute("DELETE FROM addresses WHERE id = ?", (addr["id"],))
    conn.commit()

    data = _replay(client)
    for match_id in ids:
        item = _item_for(data, match_id)
        assert item["selected_known"] is True
        assert item["selected_id"] == addr["id"]
        assert item["selected_full_text"] == target_text
        assert item["selected_missing"] is True


def test_replay_is_read_only(client):
    logs_before = client.get("/api/matches?limit=1").json()["total"]
    reviewed = client.get("/api/matches?status=reviewed&limit=1").json()["items"]
    assert reviewed, "expected at least one reviewed record"
    record_before = reviewed[0]

    _replay(client)
    _replay(client, "conflict")
    _replay(client, "consistent")

    # no new match logs were written
    assert client.get("/api/matches?limit=1").json()["total"] == logs_before
    # the reviewed record itself is untouched
    after = client.get(f"/api/matches/{record_before['id']}").json()
    assert after["status"] == "reviewed"
    assert after["review_action"] == record_before["review_action"]
    assert after["selected_id"] == record_before["selected_id"]
    assert after["score"] == record_before["score"]
    assert after["reviewed_at"] == record_before["reviewed_at"]


def test_empty_replay_on_fresh_database():
    # A throwaway database with only the sample addresses (and no reviewed
    # records at all) yields an empty replay.
    import os
    import tempfile
    from pathlib import Path

    from app import review_replay
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
        out = review_replay.compute_replay(conn, get_settings())
        assert out["items"] == []
        assert out["total_reviewed"] == 0
        assert out["consistent"] == 0
        assert out["conflict"] == 0
        assert out["missed"] == 0
        assert out["false_hit"] == 0
        assert out["risk_count"] == 0
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
