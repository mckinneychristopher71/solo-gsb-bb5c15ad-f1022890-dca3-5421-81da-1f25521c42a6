"""Tests for the manual-review claim/lease module.

Covers:
* claim issues a single ten-minute (configurable) lease token;
* claim/release run in SQLite write transactions, only one live token at a
  time, expiry frees the record for re-claim;
* single-record review requires the token and re-checks lease + pending
  state in the same transaction: missing / expired / stale token and repeat
  submission all return 409;
* a same-case group review is refused wholesale while any member is claimed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db  # noqa: E402
from app.main import app, get_conn  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _pending(client, query: str) -> int:
    r = client.post("/api/match", json={"query": query})
    data = r.json()
    assert data["status"] == "needs_review", data
    return data["match_id"]


def _claim(client, match_id: int, reviewer: str = "alice"):
    return client.post(f"/api/matches/{match_id}/claim",
                       json={"reviewer": reviewer})


def _expire_claim(match_id: int) -> None:
    """Force the stored lease into the past (avoids waiting for the TTL)."""
    conn = get_conn()
    conn.execute(
        "UPDATE review_claims SET expires_at = '2000-01-01T00:00:00+00:00' "
        "WHERE match_id = ?",
        (match_id,),
    )
    conn.commit()


def _review(client, match_id: int, token: str, action: str = "confirmed",
            selected_id: int | None = None):
    body: dict = {"action": action, "token": token}
    if selected_id is not None:
        body["selected_id"] = selected_id
    return client.post(f"/api/matches/{match_id}/review", json=body)


# ----------------------------------------------------------------- claiming


def test_claim_returns_single_token_and_hides_when_expired(client):
    mid = _pending(client, "401 Lake Dr, Austin TX")
    r = _claim(client, mid, "alice")
    assert r.status_code == 200
    data = r.json()
    assert data["match_id"] == mid
    assert data["claimed_by"] == "alice"
    assert isinstance(data["token"], str) and len(data["token"]) >= 20
    assert data["lease_seconds"] == 600
    assert data["expires_at"] > data["claimed_at"]

    # the pending queue exposes who holds it and until when
    listed = next(
        it for it in client.get("/api/matches?status=needs_review").json()["items"]
        if it["id"] == mid
    )
    assert listed["claimed_by"] == "alice"
    assert listed["claim_expires_at"] == data["expires_at"]

    # expired leases are not reported as an occupancy
    _expire_claim(mid)
    listed = next(
        it for it in client.get(f"/api/matches?status=needs_review").json()["items"]
        if it["id"] == mid
    )
    assert listed["claimed_by"] is None
    assert listed["claim_expires_at"] is None


def test_second_claimant_is_refused_until_expiry(client):
    mid = _pending(client, "402 Lake Dr, Austin TX")
    assert _claim(client, mid, "alice").status_code == 200

    # only one live token: the second reviewer gets 409, no new token
    r = _claim(client, mid, "bob")
    assert r.status_code == 409
    assert "alice" in r.json()["detail"]
    first_token = db.get_review_claim(get_conn(), mid)["token"]
    assert db.get_review_claim(get_conn(), mid)["token"] == first_token
    assert db.get_review_claim(get_conn(), mid)["claimed_by"] == "alice"

    # once the lease expires, bob can claim and gets a fresh token
    _expire_claim(mid)
    r = _claim(client, mid, "bob")
    assert r.status_code == 200
    new = r.json()
    assert new["claimed_by"] == "bob"
    assert new["token"] != first_token
    # the detail record now shows the new occupant
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["claimed_by"] == "bob"
    assert detail["claim_token"] == new["token"]


def test_claim_validates_inputs(client):
    mid = _pending(client, "403 Lake Dr, Austin TX")
    r = client.post(f"/api/matches/{mid}/claim", json={"reviewer": "   "})
    assert r.status_code == 422  # pydantic min_length after strip
    r = client.post(f"/api/matches/{mid}/claim", json={"reviewer": ""})
    assert r.status_code == 422
    # a non-existent record -> 404
    r = client.post("/api/matches/999999/claim", json={"reviewer": "alice"})
    assert r.status_code == 404


def test_claiming_a_non_pending_record_conflicts_and_cannot_be_duplicated(client):
    mid = _pending(client, "404 Lake Dr, Austin TX")
    token = _claim(client, mid).json()["token"]
    assert _review(client, mid, token).status_code == 200

    # the record is no longer pending: re-claim and repeat review are conflicts
    assert _claim(client, mid).status_code == 409
    r = _review(client, mid, token)
    assert r.status_code == 409


# ------------------------------------------------------------------ release


def test_release_requires_the_holders_token(client):
    mid = _pending(client, "405 Lake Dr, Austin TX")
    alice = _claim(client, mid, "alice").json()["token"]

    # a foreign token cannot release alice's lease
    r = client.post(f"/api/matches/{mid}/release", json={"token": "not-the-token"})
    assert r.status_code == 409
    assert db.get_review_claim(get_conn(), mid)["claimed_by"] == "alice"

    # the real token releases it, and the record can be claimed again
    r = client.post(f"/api/matches/{mid}/release", json={"token": alice})
    assert r.status_code == 200
    assert r.json()["released"] is True
    assert db.get_review_claim(get_conn(), mid) is None

    bob = _claim(client, mid, "bob").json()["token"]
    assert _review(client, mid, bob).status_code == 200

    # releasing a record that has no lease row anymore is idempotent
    r = client.post(f"/api/matches/{mid}/release", json={"token": bob})
    assert r.status_code == 200
    assert r.json()["released"] is False


def test_release_unknown_match_is_404(client):
    r = client.post("/api/matches/999999/release", json={"token": "x"})
    assert r.status_code == 404


# ------------------------------------------------------- tokenised review


def test_review_without_token_is_conflict(client):
    mid = _pending(client, "406 Lake Dr, Austin TX")
    r = client.post(f"/api/matches/{mid}/review", json={"action": "confirmed"})
    assert r.status_code == 409
    assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"


def test_review_with_old_token_after_reclaim_is_conflict(client):
    mid = _pending(client, "407 Lake Dr, Austin TX")
    old = _claim(client, mid, "alice").json()["token"]
    _expire_claim(mid)
    new = _claim(client, mid, "bob").json()["token"]
    assert old != new

    # the stale (previous) token is rejected; the record stays pending
    r = _review(client, mid, old)
    assert r.status_code == 409
    assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # the current token works and consumes the lease
    assert _review(client, mid, new).status_code == 200
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["status"] == "reviewed"
    assert detail["review_action"] == "confirmed"
    assert detail["claimed_by"] is None


def test_review_with_expired_token_is_conflict(client):
    mid = _pending(client, "408 Lake Dr, Austin TX")
    token = _claim(client, mid).json()["token"]
    _expire_claim(mid)

    r = _review(client, mid, token)
    assert r.status_code == 409
    assert "过期" in r.json()["detail"]
    assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # re-claim, then the decision goes through
    fresh = _claim(client, mid, "alice").json()["token"]
    assert _review(client, mid, fresh).status_code == 200


def test_review_snapshot_and_override_need_valid_lease(client):
    mid = _pending(client, "409 Lake Dr, Austin TX")
    token = _claim(client, mid).json()["token"]
    candidates = client.post(
        "/api/match", json={"query": "409 Lake Dr, Austin TX"}
    ).json()["candidates"]
    second = candidates[1]["address"]["id"]
    r = _review(client, mid, token, action="overridden", selected_id=second)
    assert r.status_code == 200
    detail = client.get(f"/api/matches/{mid}").json()
    assert detail["review_action"] == "overridden"
    assert detail["selected_id"] == second
    assert detail["selected_address_text"]


# ------------------------------------------- group review occupancy guard


def _same_case_pair(client, house: str) -> tuple[int, int, dict]:
    ids = [
        _pending(client, f"{house} Lake Dr, Austin TX"),
        _pending(client, f"No. {house} LAKE DR, AUSTIN TX"),
    ]
    groups = client.get("/api/review-groups").json()["groups"]
    group = next(g for g in groups if g["house"] == house
                 and set(ids).issubset(set(g["match_ids"])))
    return ids[0], ids[1], group


def test_group_review_refused_when_member_is_claimed(client):
    ids_a, ids_b, group = _same_case_pair(client, "410")
    token = _claim(client, ids_a, "alice").json()["token"]

    # group payload advertises the live claim on one of its members
    fresh = next(
        g for g in client.get("/api/review-groups").json()["groups"]
        if g["group_key"] == group["group_key"]
    )
    assert {c["match_id"] for c in fresh["claimed_members"]} == {ids_a}
    assert fresh["claimed_members"][0]["claimed_by"] == "alice"
    member = next(m for m in fresh["queries"] if m["id"] == ids_a)
    assert member["claimed_by"] == "alice"
    assert member["claim_expires_at"]

    # the whole group review is refused; nothing is written
    r = client.post("/api/review-groups/review", json={
        "group_key": fresh["group_key"], "match_ids": fresh["match_ids"],
        "action": "confirmed",
    })
    assert r.status_code == 409
    assert str(ids_a) in r.json()["detail"]
    for mid in (ids_a, ids_b):
        assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # an expired lease does not block the group any more
    _expire_claim(ids_a)
    r = client.post("/api/review-groups/review", json={
        "group_key": fresh["group_key"], "match_ids": fresh["match_ids"],
        "action": "confirmed",
    })
    assert r.status_code == 200, r.text
    for mid in (ids_a, ids_b):
        assert client.get(f"/api/matches/{mid}").json()["status"] == "reviewed"
    # the stale claim row was not blocking, token variable retained for clarity
    assert token


def test_group_review_blocked_by_live_claim_but_allowed_after_release(client):
    ids_a, ids_b, group = _same_case_pair(client, "411")
    token = _claim(client, ids_a, "carol").json()["token"]

    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"], "match_ids": group["match_ids"],
        "action": "rejected",
    })
    assert r.status_code == 409

    # releasing the individual occupancy opens the group path
    assert client.post(f"/api/matches/{ids_a}/release",
                       json={"token": token}).status_code == 200
    r = client.post("/api/review-groups/review", json={
        "group_key": group["group_key"], "match_ids": group["match_ids"],
        "action": "rejected",
    })
    assert r.status_code == 200
    for mid in (ids_a, ids_b):
        assert client.get(f"/api/matches/{mid}").json()["status"] == "reviewed"
