"""Tests for normalisation, scoring and the FastAPI end-to-end flow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402
from app.matcher import (  # noqa: E402
    levenshtein_ratio,
    match_address,
    score_house,
)
from app.normalization import normalize  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------- normalisation


def test_unicode_and_case_unification():
    n = normalize("１２３ ＭＡＩＮ ＳＴＲＥＥＴ")
    # house number is split out of the comparable text
    assert n.house == "123"
    assert "123" not in n.text
    assert "main" in n.tokens
    # street -> st
    assert "st" in n.tokens


def test_abbreviations_and_diacritics():
    n = normalize("Müller Straße 45")
    assert "muller" in n.tokens
    assert n.house == "45"


def test_house_number_formats():
    assert normalize("12 Main St").house == "12"
    assert normalize("#12-B Main St").house == "12-b"
    assert normalize("No. 12b Main Road").house == "12b"
    assert normalize("北京东路88号").house == "88"
    # 5-digit trailing value is a postcode, not a house number
    assert normalize("Main St, Austin TX 73301").house is None
    # building refs don't grab the house number
    assert normalize("99 SW 26th Ave Unit 7B, Portland").house == "99"


def test_cjk_traditional_to_simplified_and_fullwidth():
    n = normalize("北京東路８８號 3楼")
    assert n.house == "88"
    assert "东" in n.tokens          # 東 -> 东
    assert "北京" in n.tokens        # bigram


# ------------------------------------------------------------------ scoring


def test_levenshtein_ratio():
    assert levenshtein_ratio("main", "main") == 1.0
    assert levenshtein_ratio("", "x") == 0.0
    assert levenshtein_ratio("main", "mn") < 0.7


def test_house_scoring():
    assert score_house("12", "12") == 1.0
    assert score_house("12b", "12") == 0.7
    assert score_house(None, None) == 0.5
    assert score_house("12", None) == 0.15
    assert score_house("100", "1000") < 0.5
    assert score_house("12", "99") == 0.0


# ------------------------------------------------------------- end-to-end API


def test_health_and_sample_loaded(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["address_count"] >= 15


def test_web_page_and_static(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "地址匹配" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_match_abbreviation_auto(client):
    r = client.post("/api/match", json={"query": "123 main st., New York, NY 10001"})
    data = r.json()
    assert data["status"] == "auto_matched"
    assert data["best_id"] == 1
    assert data["best_score"] >= 0.8


def test_match_house_conflict_needs_review(client):
    r = client.post("/api/match", json={"query": "1010 Lake Dr, Austin TX"})
    data = r.json()
    # street tokens match fully but the closest house number differs -> review
    best = data["candidates"][0]
    assert best["token_f1"] >= 0.99
    assert data["status"] == "needs_review"
    conflict_fields = {c["field"] for c in best["conflicts"]}
    assert "house_number" in conflict_fields


def test_match_with_unit_suffix(client):
    r = client.post("/api/match",
                    json={"query": "99 SW 26th Ave Unit 7B, Portland OR"})
    data = r.json()
    assert data["normalized"]["house"] == "99"
    assert data["best_id"] == 14
    assert data["status"] == "auto_matched"


def test_match_terrace_abbreviation(client):
    r = client.post("/api/match", json={"query": "742 Evergreen Tce"})
    data = r.json()
    assert data["best_id"] == 15
    assert data["status"] == "auto_matched"


def test_match_chinese(client):
    r = client.post("/api/match", json={"query": "北京東路８８號 3楼, 上海"})
    data = r.json()
    assert data["normalized"]["house"] == "88"
    assert data["best_id"] == 10
    assert data["status"] == "auto_matched"


def test_match_no_match(client):
    r = client.post("/api/match", json={"query": "1 Milky Way, Mars Base"})
    assert r.json()["status"] == "no_match"


def test_batch_and_review_flow(client):
    r = client.post("/api/match/batch", json={
        "queries": ["Hillcrest Road, Dallas", "123 main st ny"],
    })
    data = r.json()
    assert data["summary"]["total"] == 2
    # ambiguous query (50 vs 52 Hillcrest, no house number) needs review
    ambiguous = data["results"][0]
    assert ambiguous["status"] == "needs_review"
    log_id = ambiguous["match_id"]

    # review queue lists it
    r = client.get("/api/matches", params={"status": "needs_review"})
    ids = [item["id"] for item in r.json()["items"]]
    assert log_id in ids

    # confirm the system's preferred candidate (claim first, then review)
    r = _claim_and_review(client, log_id, {"action": "confirmed"})
    assert r.status_code == 200
    r = client.get(f"/api/matches/{log_id}")
    assert r.json()["status"] == "reviewed"
    assert r.json()["review_action"] == "confirmed"


def _claim_and_review(client, match_id: int, body: dict):
    """Claim the record (10-minute lease) and submit the review with the token."""
    r = client.post(f"/api/matches/{match_id}/claim", json={"reviewer": "pytest"})
    assert r.status_code == 200, r.text
    return client.post(
        f"/api/matches/{match_id}/review",
        json={**body, "token": r.json()["token"]},
    )


def test_override_review(client):
    r = client.post("/api/match", json={"query": "Hillcrest Rd, Dallas TX"})
    log_id = r.json()["match_id"]
    candidates = r.json()["candidates"]
    second = candidates[1]["address"]["id"]
    r = _claim_and_review(client, log_id,
                          {"action": "overridden", "selected_id": second})
    assert r.status_code == 200
    detail = client.get(f"/api/matches/{log_id}").json()
    assert detail["review_action"] == "overridden"
    assert detail["selected_id"] == second


def test_csv_upload_and_dedup(client):
    csv_bytes = b"id,full_address,town,zip\nX1,9 Test Lane,Testville,99999\n"
    r = client.post("/api/addresses/upload",
                    files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    r2 = client.post("/api/addresses/upload",
                     files={"file": ("x.csv", csv_bytes, "text/csv")})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 1

    r = client.post("/api/match", json={"query": "9 Test Ln, Testville 99999"})
    assert r.json()["best_id"] is not None


# ----------------------------------------------------- CSV preview / confirm


def _preview_csv(prefix: str) -> bytes:
    # line 2 valid, line 3 invalid (all blanks), line 4 valid,
    # line 5 an in-file repeat of line 2 (duplicate).
    return (
        "external_id,address,city,zip\n"
        f"{prefix}-001,1 Preview Way,Previewton,10001\n"
        ",,,\n"
        f"{prefix}-002,2 Preview Ave,Previewton,10002\n"
        f"{prefix}-001,1 Preview Way,Previewton,10001\n"
    ).encode()


def test_preview_then_confirm_writes_only_valid(client):
    csv_bytes = _preview_csv("PA")
    before = client.get("/api/health").json()["address_count"]
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] == 2
    assert data["duplicates"] == 1
    assert data["invalid"] == 1
    assert data["invalid_lines"] == [3]
    assert data["total"] == 4
    token = data["preview_token"]

    # Preview must not have written anything yet.
    assert client.get("/api/health").json()["address_count"] == before

    # Confirm requires the file to be re-submitted alongside the token.
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 200
    result = r.json()
    assert result["inserted"] == 2
    assert result["duplicates"] == 1
    assert result["invalid"] == 1
    assert result["invalid_lines"] == [3]
    assert client.get("/api/health").json()["address_count"] == before + 2

    # The voucher is single-use: confirming again fails.
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 404


def test_preview_uses_header_aliases(client):
    csv_bytes = b"id,addr,town,postcode\nPAL,7 Alpha Road,Alphaville,20001\n"
    r = client.post("/api/addresses/preview",
                    files={"file": ("a.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] == 1
    assert data["invalid"] == 0
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("a.csv", csv_bytes, "text/csv")},
        data={"preview_token": data["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 1


def test_preview_confirm_rejects_changed_file(client):
    csv_bytes = _preview_csv("PB")
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    token = r.json()["preview_token"]
    before = client.get("/api/health").json()["address_count"]

    # A different file body carrying the same token must be refused.
    tampered = csv_bytes.replace(b"2 Preview Ave", b"99 Other Street")
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", tampered, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 409
    # Nothing was written.
    assert client.get("/api/health").json()["address_count"] == before

    # The voucher is not consumed by the failed attempt, but the original file
    # was never confirmed; re-preview fresh (unique ids) data and confirm it.
    fresh = (
        "external_id,address\nPB2-001,3 Fresh Court\nPB2-002,4 Fresh Plaza\n"
    ).encode()
    r = client.post("/api/addresses/preview",
                    files={"file": ("f.csv", fresh, "text/csv")})
    assert r.status_code == 200
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("f.csv", fresh, "text/csv")},
        data={"preview_token": r.json()["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2


def test_preview_confirm_rejects_changed_table(client):
    csv_bytes = _preview_csv("PC")
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.csv", csv_bytes, "text/csv")})
    token = r.json()["preview_token"]
    before = client.get("/api/health").json()["address_count"]

    # Another writer (the legacy direct-upload endpoint) mutates the table.
    other = b"id,address\nPCINT,8 Interloper Lane\n"
    assert client.post("/api/addresses/upload",
                       files={"file": ("o.csv", other, "text/csv")}).status_code == 200

    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", csv_bytes, "text/csv")},
        data={"preview_token": token},
    )
    assert r.status_code == 409
    # Only the interloper was written; the stale preview wrote nothing.
    assert client.get("/api/health").json()["address_count"] == before + 1

    # Re-preview fresh (unique ids) data against the new snapshot, then confirm.
    fresh = (
        "external_id,address\nPC2-001,5 New Crescent\nPC2-002,6 New Gardens\n"
    ).encode()
    r = client.post("/api/addresses/preview",
                    files={"file": ("f.csv", fresh, "text/csv")})
    assert r.status_code == 200
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("f.csv", fresh, "text/csv")},
        data={"preview_token": r.json()["preview_token"]},
    )
    assert r.status_code == 200
    assert r.json()["inserted"] == 2


def test_preview_confirm_unknown_token(client):
    r = client.post(
        "/api/addresses/confirm",
        files={"file": ("p.csv", _preview_csv("PD"), "text/csv")},
        data={"preview_token": "does-not-exist"},
    )
    assert r.status_code == 404


def test_preview_rejects_non_csv(client):
    r = client.post("/api/addresses/preview",
                    files={"file": ("p.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_preview_invalid_lines_use_physical_numbers(client):
    # A blank physical line between data rows is skipped by the csv module and
    # not counted; the trailing invalid row still reports its true line number.
    content = (
        b"external_id,address\n"   # line 1 header
        b"PLN1,11 Line Lane\n"     # line 2 valid
        b"\n"                       # line 3 blank -> skipped
        b",\n"                      # line 4 invalid
    )
    r = client.post("/api/addresses/preview",
                    files={"file": ("l.csv", content, "text/csv")})
    data = r.json()
    assert data["valid"] == 1
    assert data["invalid"] == 1
    assert data["invalid_lines"] == [4]


# ------------------------------------------------- same-case merged review


def _match_pending(client, query: str) -> int:
    r = client.post("/api/match", json={"query": query})
    data = r.json()
    assert data["status"] == "needs_review", data
    return data["match_id"]


def _group_by_house(client, house: str) -> dict:
    r = client.get("/api/review-groups")
    assert r.status_code == 200
    matches = [g for g in r.json()["groups"] if g["house"] == house]
    assert len(matches) == 1, matches
    return matches[0]


def test_review_groups_merge_same_case_and_confirm(client):
    # Three differently-written queries that normalise to the same
    # road/locality tokens + house number and therefore share one group.
    ids = [
        _match_pending(client, "300 Lake Dr, Austin TX"),
        _match_pending(client, "No. 300 Lake Dr, Austin TX"),
        _match_pending(client, "300 LAKE DR, AUSTIN TX"),
    ]
    g = _group_by_house(client, "300")
    assert g["count"] == 3
    assert sorted(g["match_ids"]) == sorted(ids)
    # the group exposes the original queries of its members
    assert {m["query"] for m in g["queries"]} == {
        "300 Lake Dr, Austin TX",
        "No. 300 Lake Dr, Austin TX",
        "300 LAKE DR, AUSTIN TX",
    }
    assert g["street_tokens"] == ["dr", "lake"]
    assert g["locality_tokens"] == ["austin", "tx"]

    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": g["match_ids"],
        "action": "confirmed",
    })
    assert r.status_code == 200
    assert r.json()["reviewed"] == 3
    for mid in ids:
        d = client.get(f"/api/matches/{mid}").json()
        assert d["status"] == "reviewed"
        assert d["review_action"] == "confirmed"
        assert d["selected_id"] == d["candidate_id"]

    # the whole group left the pending queue
    houses = [g2["house"] for g2 in client.get("/api/review-groups").json()["groups"]]
    assert "300" not in houses


def test_review_group_override_and_stale_member_set(client):
    ids = [
        _match_pending(client, "301 Lake Dr, Austin TX"),
        _match_pending(client, "301 Lake Drive, Austin TX"),
    ]
    g = _group_by_house(client, "301")
    assert sorted(g["match_ids"]) == sorted(ids)
    second = g["candidates"][1]["address"]["id"]
    second_score = g["candidates"][1]["score"]

    # stale member set -> conflict, and nothing is written
    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": ids + [999999],
        "action": "overridden", "selected_id": second,
    })
    assert r.status_code == 409
    for mid in ids:
        assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # the override target must be one of the shared candidates
    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": ids,
        "action": "overridden", "selected_id": 1,
    })
    assert r.status_code == 400
    for mid in ids:
        assert client.get(f"/api/matches/{mid}").json()["status"] == "needs_review"

    # the real override applies the same decision + score to every member
    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": ids,
        "action": "overridden", "selected_id": second,
    })
    assert r.status_code == 200
    assert r.json()["reviewed"] == 2
    for mid in ids:
        d = client.get(f"/api/matches/{mid}").json()
        assert d["status"] == "reviewed"
        assert d["review_action"] == "overridden"
        assert d["selected_id"] == second
        assert d["score"] == second_score


def test_review_group_member_reviewed_individually_conflicts(client):
    ids = [
        _match_pending(client, "302 Lake Dr, Austin TX"),
        _match_pending(client, "302 LAKE DR, AUSTIN TX"),
    ]
    g = _group_by_house(client, "302")

    # one member gets reviewed through the original single-record endpoint
    r = _claim_and_review(client, ids[0], {"action": "rejected"})
    assert r.status_code == 200

    # the group review now conflicts and writes nothing at all
    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": ids, "action": "confirmed",
    })
    assert r.status_code == 409
    assert client.get(f"/api/matches/{ids[1]}").json()["status"] == "needs_review"


def test_review_group_reject(client):
    ids = [
        _match_pending(client, "303 Lake Dr, Austin TX"),
        _match_pending(client, "No. 303 Lake Dr, Austin TX"),
    ]
    g = _group_by_house(client, "303")
    r = client.post("/api/review-groups/review", json={
        "group_key": g["group_key"], "match_ids": ids, "action": "rejected",
    })
    assert r.status_code == 200
    assert r.json()["reviewed"] == 2
    for mid in ids:
        d = client.get(f"/api/matches/{mid}").json()
        assert d["status"] == "reviewed"
        assert d["review_action"] == "rejected"
        assert d["selected_id"] is None


def test_review_groups_split_when_candidates_differ(client):
    # Same normalised key, but the address table changes in between so the
    # candidate IDs/order/scores differ -> the records must NOT share a group.
    first_id = _match_pending(client, "304 Lake Dr, Austin TX")
    r = client.post("/api/addresses", json={"addresses": [
        {"address": "305 Lake Drive, Austin, TX 73301", "external_id": "GRP-305"},
    ]})
    assert r.json()["inserted"] == 1
    second_id = _match_pending(client, "304 LAKE DR, AUSTIN TX")

    groups = [g for g in client.get("/api/review-groups").json()["groups"]
              if g["house"] == "304"]
    assert len(groups) == 2
    assert all(g["count"] == 1 for g in groups)
    assert {g["match_ids"][0] for g in groups} == {first_id, second_id}
    assert groups[0]["group_key"] != groups[1]["group_key"]

    # a singleton group can still be reviewed as a group (member set of one)
    g1 = next(g for g in groups if g["match_ids"] == [first_id])
    r = client.post("/api/review-groups/review", json={
        "group_key": g1["group_key"], "match_ids": [first_id],
        "action": "confirmed",
    })
    assert r.status_code == 200
    assert r.json()["reviewed"] == 1
    assert client.get(f"/api/matches/{first_id}").json()["status"] == "reviewed"


# ------------------------------------------------- review consensus memory


def _seed_two_confirms(client, query_a: str, query_b: str) -> int:
    """Review two different spellings of the same case, both -> best candidate."""
    ids = []
    target_id = None
    for q in (query_a, query_b):
        r = client.post("/api/match", json={"query": q})
        data = r.json()
        assert data["status"] == "needs_review", data
        ids.append(data["match_id"])
        target_id = data["best_id"]
    for mid in ids:
        r = _claim_and_review(client, mid, {"action": "confirmed"})
        assert r.status_code == 200
    assert target_id is not None
    return target_id


def test_consensus_auto_applies_after_two_confirms(client):
    # Two spelling variants normalise to the same road/locality/house key and
    # are both human-confirmed; the third variant is resolved automatically.
    target = _seed_two_confirms(
        client, "1010 Lake Dr, Austin TX", "No. 1010 Lake Drive, Austin TX"
    )

    r = client.post("/api/match", json={"query": "1010 LAKE DR, AUSTIN TX"})
    data = r.json()
    assert data["status"] == "auto_matched"
    assert data["best_id"] == target
    con = data["consensus"]
    assert con["state"] == "hit"
    assert con["applied"] is True
    assert con["target_id"] == target
    assert con["evidence_count"] >= 2
    assert con["rejected_count"] == 0
    # the original scoring verdict is preserved inside the consensus block
    assert con["original_status"] == "needs_review"

    # it is persisted as auto_matched with the consensus verdict attached
    detail = client.get(f"/api/matches/{data['match_id']}").json()
    assert detail["status"] == "auto_matched"
    assert detail["consensus"]["applied"] is True
    assert detail["consensus"]["target_id"] == target


def test_consensus_single_record_is_insufficient(client):
    mid = _match_pending(client, "1011 Lake Dr, Austin TX")
    _claim_and_review(client, mid, {"action": "confirmed"})

    data = client.post("/api/match", json={"query": "1011 Lake Dr, Austin TX"}).json()
    assert data["status"] == "needs_review"
    con = data["consensus"]
    assert con["state"] == "insufficient"
    assert con["applied"] is False
    assert con["evidence_count"] == 1


def test_consensus_blocked_by_rejection(client):
    mid_yes = _match_pending(client, "1012 Lake Dr, Austin TX")
    _claim_and_review(client, mid_yes, {"action": "confirmed"})
    mid_no = _match_pending(client, "No. 1012 Lake Dr, Austin TX")
    _claim_and_review(client, mid_no, {"action": "rejected"})

    data = client.post("/api/match", json={"query": "1012 LAKE DR, AUSTIN TX"}).json()
    # one rejection poisons the bucket even with another confirmation present
    assert data["status"] == "needs_review"
    con = data["consensus"]
    assert con["state"] == "conflict"
    assert con["applied"] is False
    assert con["rejected_count"] == 1


def test_consensus_blocked_by_conflicting_targets(client):
    first = client.post("/api/match", json={"query": "1017 Lake Dr, Austin TX"}).json()
    target_a = first["best_id"]
    target_b = first["candidates"][1]["address"]["id"]
    assert target_a != target_b

    mid_a = first["match_id"]
    mid_b = client.post("/api/match",
                        json={"query": "No. 1017 Lake Dr, Austin TX"}).json()["match_id"]
    _claim_and_review(client, mid_a, {"action": "confirmed"})
    _claim_and_review(client, mid_b,
                      {"action": "overridden", "selected_id": target_b})

    data = client.post("/api/match", json={"query": "1017 LAKE DR, AUSTIN TX"}).json()
    assert data["status"] == "needs_review"
    con = data["consensus"]
    assert con["state"] == "conflict"
    assert con["applied"] is False


def test_consensus_takes_effect_immediately_and_does_not_rewrite(client):
    mid_a = _match_pending(client, "1014 Lake Dr, Austin TX")
    mid_b = _match_pending(client, "No. 1014 Lake Dr, Austin TX")

    # before the reviews: not enough evidence, stays pending
    before = client.post("/api/match", json={"query": "1014 LAKE DR, AUSTIN TX"}).json()
    assert before["status"] == "needs_review"
    assert before["consensus"]["state"] == "insufficient"

    _claim_and_review(client, mid_a, {"action": "confirmed"})
    # still just one -> insufficient
    one = client.post("/api/match", json={"query": "1014 Lake Drive, Austin TX"}).json()
    assert one["consensus"]["state"] == "insufficient"

    _claim_and_review(client, mid_b, {"action": "confirmed"})
    # new reviews take effect immediately
    after = client.post("/api/match", json={"query": "1014 LAKE DR, AUSTIN TX"}).json()
    assert after["status"] == "auto_matched"
    assert after["consensus"]["applied"] is True

    # old records are never rewritten
    for mid in (mid_a, mid_b):
        old = client.get(f"/api/matches/{mid}").json()
        assert old["status"] == "reviewed"
        assert old["review_action"] == "confirmed"


def test_consensus_in_batch(client):
    target = _seed_two_confirms(
        client, "1018 Lake Dr, Austin TX", "No. 1018 Lake Dr, Austin TX"
    )
    r = client.post("/api/match/batch", json={
        "queries": ["1018 LAKE DR, AUSTIN TX", "zzqxkw marsport 99999"],
    })
    consensus_hit, no_match = r.json()["results"]
    assert consensus_hit["status"] == "auto_matched"
    assert consensus_hit["best_id"] == target
    assert consensus_hit["consensus"]["applied"] is True
    # gibberish verdicts are never redirected by consensus
    assert no_match["status"] == "no_match"
    s = r.json()["summary"]
    assert s["auto_matched"] == 1
    assert s["no_match"] == 1


def test_consensus_target_not_in_candidates_keeps_verdict():
    # Unanimous history pointing at address #1, while the current match's
    # candidate list contains different addresses: original verdict stands.
    from app import consensus as consensus_mod
    from app.main import get_conn
    from app.matcher import CandidateResult, MatchResult
    from app.normalization import Normalized

    conn = get_conn()
    ver = consensus_mod.lookup_consensus(
        conn, "123 Main Street, New York, NY 10001"
    )
    # No reviews yet for that key: force a unanimous-hit verdict manually and
    # feed it to apply_consensus with a candidate set that excludes the target.
    hit = {
        **ver,
        "state": "hit",
        "applied": False,
        "target_id": 1,
        "target": {"id": 1, "full_text": "123 Main Street"},
        "evidence_count": 2,
        "rejected_count": 0,
        "reason": "x",
    }
    result = MatchResult(
        query="123 Main Street, New York, NY 10001",
        normalized_query=Normalized(original="", text=""),
        status="needs_review",
        reason="ambiguous",
        best=None,
        runner_up=None,
        candidates=[],
        thresholds={},
    )
    consensus_mod.apply_consensus(result, hit)
    assert result.status == "needs_review"
    assert result.consensus["state"] == "not_candidate"
    assert result.consensus["applied"] is False
    assert "不在本次候选" in result.consensus["reason"]


def test_consensus_target_address_deleted_keeps_verdict():
    from app import consensus as consensus_mod
    from app import db
    from app.main import get_conn

    conn = get_conn()
    # Insert two reviewed logs for a brand-new normalised key, both pointing
    # at a throwaway address which is then deleted.
    address_id = db.insert_address(
        conn,
        full_text="88 Consensus Way, Consensus City",
        tokens=["consensus", "way"],
        street_tokens=["consensus", "way"],
        locality_tokens=["consensus", "city"],
        house="88",
        fields={},
        raw={},
    )
    for q in ("88 Consensus Way, Consensus City", "No. 88 Consensus Way, Consensus City"):
        log_id = db.insert_match_log(
            conn,
            query=q,
            status="needs_review",
            score=0.5,
            candidate_id=address_id,
            token_score=0.6,
            house_score=0.3,
            matched_tokens=[],
            conflicts=[],
            candidates=[],
        )
        # Review through the persistence layer so the durable snapshot (ID +
        # text) is written just like the single/group review APIs do.
        db.review_match_log(
            conn, log_id, action="confirmed",
            selected_id=address_id, score=0.5,
            selected_text="88 Consensus Way, Consensus City",
        )
    conn.commit()
    conn.execute("DELETE FROM addresses WHERE id = ?", (address_id,))
    conn.commit()

    ver = consensus_mod.lookup_consensus(conn, "88 CONSENSUS WAY, CONSENSUS CITY")
    assert ver["state"] == "target_gone"
    assert ver["applied"] is False
    # The FK is nulled, but the durable snapshot still identifies the deleted
    # target, so the evidence remains attributable (not stale/unknown).
    assert ver["stale_count"] == 0
    assert ver["evidence_count"] == 2
    assert ver["target_id"] == address_id
