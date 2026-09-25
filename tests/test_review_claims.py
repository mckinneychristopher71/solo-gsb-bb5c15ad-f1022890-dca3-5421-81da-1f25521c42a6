"""Tests for the manual-review claim leases (领取 / 释放 / 令牌复核)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_conn  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _pending(client, query: str) -> int:
    """Create a fresh needs_review record and return its match id."""
    r = client.post("/api/match", json={"query": query})
    data = r.json()
    assert data["status"] == "needs_review", data
    return data["match_id"]


def _claim(client, match_id: int, reviewer: str = "张三"):
    return client.post(f"/api/matches/{match_id}/claim",
                       json={"reviewer": reviewer})


def _review(client, match_id: int, body: dict):
    return client.post(f"/api/matches/{match_id}/review", json=body)


def _force_expire(match_id: int, token: str) -> None:
    """Backdate the lease so the token is expired (deterministic, no sleeps)."""
    conn = get_conn()
    conn.execute(
        "UPDATE review_claims SET expires_at = ? WHERE match_id = ? AND token = ?",
        ("2000-01-01T00:00:00+00:00", match_id, token),
    )
    conn.commit()


# ------------------------------------------------------------------ claiming


def test_claim_happy_path_and_list_shows_claim(client):
    mid = _pending(client, "600 Lake Dr, Austin TX")

    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["claim"] is None

    r = _claim(client, mid)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["match_id"] == mid
    assert data["reviewer"] == "张三"
    assert data["lease_seconds"] == 600
    assert data["token"]

    # the lease is ten minutes: expires_at ~ now + 600s
    expires = datetime.fromisoformat(data["expires_at"])
    delta = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 580 <= delta <= 600

    # the record detail and the queue list both expose reviewer + lease end
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["claim"]["reviewer"] == "张三"
    assert detail["claim"]["expires_at"] == data["expires_at"]
    assert "token" not in detail["claim"]  # never exposed after issuance

    items = client.get("/api/matches?status=needs_review&limit=200").json()["items"]
    item = next(i for i in items if i["id"] == mid)
    assert item["claim"]["reviewer"] == "张三"
    assert item["claim"]["expires_at"] == data["expires_at"]

    # clean up: release so later tests see an unclaimed record
    token = data["token"]
    assert client.post(f"/api/matches/{mid}/release",
                       json={"token": token}).status_code == 200


def test_double_claim_conflict(client):
    mid = _pending(client, "601 Lake Dr, Austin TX")
    assert _claim(client, mid, "张三").status_code == 200

    # a second reviewer cannot take the record while the lease is valid
    r = _claim(client, mid, "李四")
    assert r.status_code == 409
    assert "张三" in r.json()["detail"]

    # even the same reviewer cannot stack a second lease on top
    assert _claim(client, mid, "张三").status_code == 409


def test_claim_requires_pending_status(client):
    # unknown record -> 404
    assert _claim(client, 999999999).status_code == 404

    # auto-matched records are not claimable
    m = client.post("/api/match", json={"query": "123 main st ny"}).json()
    assert m["status"] == "auto_matched"
    r = _claim(client, m["match_id"])
    assert r.status_code == 409
    assert "待确认" in r.json()["detail"]


def test_claim_reviewer_name_validated(client):
    mid = _pending(client, "602 Lake Dr, Austin TX")
    assert client.post(f"/api/matches/{mid}/claim",
                       json={"reviewer": "   "}).status_code == 422
    assert client.post(f"/api/matches/{mid}/claim", json={}).status_code == 422


# ------------------------------------------------------------------ releasing


def test_release_and_reclaim(client):
    mid = _pending(client, "603 Lake Dr, Austin TX")
    token1 = _claim(client, mid).json()["token"]

    # unknown token -> 404; the lease stays valid
    assert client.post(f"/api/matches/{mid}/release",
                       json={"token": "no-such-token"}).status_code == 404

    # the valid token releases the lease
    r = client.post(f"/api/matches/{mid}/release", json={"token": token1})
    assert r.status_code == 200
    assert r.json()["released"] is True
    assert client.get(f"/api/matches/{mid}").json()["claim"] is None

    # the released token is stale: it cannot be released twice
    assert client.post(f"/api/matches/{mid}/release",
                       json={"token": token1}).status_code == 409

    # after a release the record can immediately be claimed again
    r = _claim(client, mid, "李四")
    assert r.status_code == 200
    token2 = r.json()["token"]
    assert token2 != token1
    assert client.get(f"/api/matches/{mid}").json()["claim"]["reviewer"] == "李四"

    # release on an unknown record -> 404
    assert client.post("/api/matches/999999999/release",
                       json={"token": token2}).status_code == 404


# ------------------------------------------------------------- token reviews


def test_review_requires_valid_token(client):
    mid = _pending(client, "604 Lake Dr, Austin TX")

    # no token at all -> 409 (the submission must carry the lease token)
    r = _review(client, mid, {"action": "confirmed"})
    assert r.status_code == 409

    # a made-up token -> 409
    r = _review(client, mid, {"action": "confirmed", "token": "bogus"})
    assert r.status_code == 409

    # claim, then the same submission succeeds
    token = _claim(client, mid).json()["token"]
    r = _review(client, mid, {"action": "confirmed", "token": token})
    assert r.status_code == 200
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["status"] == "reviewed"
    assert detail["review_action"] == "confirmed"
    assert detail["selected_id"] == detail["candidate_id"]
    # the lease was consumed by the submitted review
    assert detail["claim"] is None


def test_review_duplicate_submission_conflict(client):
    mid = _pending(client, "605 Lake Dr, Austin TX")
    token = _claim(client, mid).json()["token"]
    assert _review(client, mid, {"action": "confirmed", "token": token}).status_code == 200

    # the exact same submission again -> 409 (record no longer pending)
    r = _review(client, mid, {"action": "confirmed", "token": token})
    assert r.status_code == 409

    # the consumed token cannot be reused even for a different action
    r = _review(client, mid, {"action": "rejected", "token": token})
    assert r.status_code == 409

    # and a reviewed record cannot be claimed again
    assert _claim(client, mid).status_code == 409


def test_expired_then_old_token_rejected(client):
    mid = _pending(client, "606 Lake Dr, Austin TX")
    token1 = _claim(client, mid, "张三").json()["token"]
    _force_expire(mid, token1)

    # expired token: review and release are both refused
    r = _review(client, mid, {"action": "confirmed", "token": token1})
    assert r.status_code == 409
    assert "过期" in r.json()["detail"]
    assert client.post(f"/api/matches/{mid}/release",
                       json={"token": token1}).status_code == 409

    # after expiry the record can be claimed again -> a fresh token
    r = _claim(client, mid, "李四")
    assert r.status_code == 200
    token2 = r.json()["token"]
    assert token2 != token1

    # the old token stays rejected (stale), the new one works
    assert _review(client, mid, {"action": "confirmed", "token": token1}).status_code == 409
    r = _review(client, mid, {"action": "confirmed", "token": token2})
    assert r.status_code == 200
    assert client.get(f"/api/matches/{mid}").json()["status"] == "reviewed"


def test_review_override_with_token(client):
    mid = _pending(client, "607 Lake Dr, Austin TX")
    detail = client.get(f"/api/matches/{mid}").json()
    second = detail["candidates"][1]["address"]["id"]
    token = _claim(client, mid).json()["token"]
    r = _review(client, mid,
                {"action": "overridden", "selected_id": second, "token": token})
    assert r.status_code == 200
    assert r.json()["selected_id"] == second
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["review_action"] == "overridden"
    assert detail["selected_id"] == second


def test_review_unknown_record_404(client):
    r = _review(client, 999999999, {"action": "confirmed", "token": "x"})
    assert r.status_code == 404


# ------------------------------------------------------- group review blocking


def test_group_review_rejected_while_member_claimed(client):
    ids = [
        _pending(client, "608 Lake Dr, Austin TX"),
        _pending(client, "No. 608 Lake Dr, Austin TX"),
    ]
    groups = client.get("/api/review-groups").json()["groups"]
    group = next(g for g in groups if set(ids).issubset(set(g["match_ids"])))
    assert sorted(group["match_ids"]) == sorted(ids)

    # claim one member; the group listing exposes the occupancy
    token = _claim(client, ids[0], "张三").json()["token"]
    groups = client.get("/api/review-groups").json()["groups"]
    group = next(g for g in groups if set(ids).issubset(set(g["match_ids"])))
    by_id = {q["id"]: q for q in group["queries"]}
    assert by_id[ids[0]]["claimed_by"] == "张三"
    assert by_id[ids[0]]["claim_expires_at"]
    assert by_id[ids[1]]["claimed_by"] is None

    # the whole group review is refused and nothing is written
    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"], "match_ids": ids, "action": "confirmed",
    })
    assert r.status_code == 409
    assert "张三" in r.json()["detail"]
    for mid in ids:
        assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # after the lease is released the same group review succeeds
    assert client.post(f"/api/matches/{ids[0]}/release",
                       json={"token": token}).status_code == 200
    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"], "match_ids": ids, "action": "confirmed",
    })
    assert r.status_code == 200
    assert r.json()["reviewed"] == 2
    for mid in ids:
        assert client.get(f"/api/matches/{mid}").json()["status"] == "reviewed"


def test_group_review_allowed_after_lease_expires(client):
    ids = [
        _pending(client, "609 Lake Dr, Austin TX"),
        _pending(client, "No. 609 Lake Dr, Austin TX"),
    ]
    token = _claim(client, ids[0]).json()["token"]
    _force_expire(ids[0], token)

    groups = client.get("/api/review-groups").json()["groups"]
    group = next(g for g in groups if set(ids).issubset(set(g["match_ids"])))
    # expired claims no longer show up as occupancy
    assert all(q["claimed_by"] is None for q in group["queries"])

    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"], "match_ids": ids, "action": "rejected",
    })
    assert r.status_code == 200
    for mid in ids:
        d = client.get(f"/api/matches/{mid}").json()
        assert d["status"] == "reviewed"
        assert d["review_action"] == "rejected"
