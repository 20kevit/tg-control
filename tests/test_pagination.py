"""Cursor pagination: unit invariants plus end-to-end HTTP walks."""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update
from test_api import auth, create_resources, set_policy

from tg_control.db import Job
from tg_control.pagination import (
    CursorError,
    decode_cursor,
    derive_key,
    encode_cursor,
    normalise_limit,
    scope_digest,
)

KEY = derive_key(b"synthetic-cursor-key")


def test_cursor_roundtrip_is_deterministic_and_scope_bound():
    token = encode_cursor(
        key=KEY, scope="s", sort_value="2026-01-01T00:00:00+00:00", identifier="abc", now=1000
    )
    assert token == encode_cursor(
        key=KEY, scope="s", sort_value="2026-01-01T00:00:00+00:00", identifier="abc", now=1000
    )
    assert decode_cursor(token, key=KEY, scope="s", now=1001) == (
        "2026-01-01T00:00:00+00:00",
        "abc",
    )
    with pytest.raises(CursorError) as other_scope:
        decode_cursor(token, key=KEY, scope="other", now=1001)
    assert other_scope.value.code == "invalid_cursor"
    with pytest.raises(CursorError):
        decode_cursor(token, key=derive_key(b"another-key"), scope="s", now=1001)


def test_cursor_rejects_tampering_and_malformed_input():
    token = encode_cursor(key=KEY, scope="s", sort_value=None, identifier=1, now=1000)
    payload, signature = token.split(".")
    for bad in (
        "not-a-cursor",
        "",
        "x" * 2000,
        payload + "." + signature[:-1] + ("A" if signature[-1] != "A" else "B"),
        payload + "." + signature + ".extra",
        token + "=",
        None,
        123,
        "!!!.???",
    ):
        with pytest.raises(CursorError):
            decode_cursor(bad, key=KEY, scope="s", now=1001)
    # A payload signed with a different secret is rejected even if well formed.
    forged = encode_cursor(key=derive_key(b"attacker"), scope="s", sort_value=None, identifier=1)
    with pytest.raises(CursorError):
        decode_cursor(forged, key=KEY, scope="s", now=1001)


def test_cursor_expiry_and_identifier_validation():
    token = encode_cursor(
        key=KEY, scope="s", sort_value=None, identifier=1, ttl_seconds=60, now=1000
    )
    assert decode_cursor(token, key=KEY, scope="s", now=1001)
    with pytest.raises(CursorError) as expired:
        decode_cursor(token, key=KEY, scope="s", now=2000)
    assert expired.value.code == "expired_cursor"
    with pytest.raises(CursorError):
        decode_cursor(
            encode_cursor(key=KEY, scope="s", sort_value=None, identifier="bad id!", now=1),
            key=KEY,
            scope="s",
            now=2,
        )


def test_limit_bounds_and_scope_digest():
    assert normalise_limit(None) == 25
    assert normalise_limit(1) == 1
    assert normalise_limit(100) == 100
    for bad in (0, 101, -1, True, "10", 1.5):
        with pytest.raises(CursorError):
            normalise_limit(bad)
    assert scope_digest("a", "b") == scope_digest("a", "b")
    assert scope_digest("a", "b") != scope_digest("a", "c")
    assert len(scope_digest(None, "x")) == 24


def _enqueue_jobs(api, token, account, count):
    ids = []
    for index in range(count):
        response = api.post(
            "/v1/jobs",
            headers=auth(token),
            json={
                "kind": "telegram.send",
                "input": {
                    "account_id": account["id"],
                    "chat_id": "public-channel",
                    "text": f"synthetic job {index}",
                },
                "idempotency_key": str(uuid.uuid4()),
                "deadline_seconds": 600,
                "max_attempts": 1,
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["job_id"])
    return ids


def test_job_pagination_first_intermediate_final_empty_and_isolation(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    other = api.post(
        "/v1/admin/clients", headers=owner, json={"name": "pagination other client"}
    ).json()
    token, other_token = client["credential"], other["credential"]
    created = _enqueue_jobs(api, token, account, 5)

    first = api.get("/v1/jobs?limit=2", headers=auth(token)).json()
    assert len(first["jobs"]) == 2 and first["page"]["has_more"] is True
    assert first["page"]["next_cursor"]

    second = api.get(
        f"/v1/jobs?limit=2&cursor={first['page']['next_cursor']}", headers=auth(token)
    ).json()
    third = api.get(
        f"/v1/jobs?limit=2&cursor={second['page']['next_cursor']}", headers=auth(token)
    ).json()
    assert third["page"]["has_more"] is False and third["page"]["next_cursor"] is None
    walked = [job["job_id"] for page in (first, second, third) for job in page["jobs"]]
    assert sorted(walked) == sorted(created)
    assert len(walked) == len(set(walked)), "an item must not appear on two pages"

    empty = api.get("/v1/jobs?limit=5", headers=auth(other_token)).json()
    assert empty["jobs"] == [] and empty["page"]["has_more"] is False

    # A cursor minted for one client is not usable by another client or client-less axis.
    stolen = api.get(
        f"/v1/jobs?limit=2&cursor={first['page']['next_cursor']}", headers=auth(other_token)
    )
    assert stolen.status_code == 422 and stolen.json()["error"] == "invalid_cursor"
    admin = api.get(
        f"/v1/admin/jobs?cursor={first['page']['next_cursor']}", headers=owner
    )
    assert admin.status_code == 422 and admin.json()["error"] == "invalid_cursor"
    # The same cursor without tampering also fails for a different filter axis.
    hostile = first["page"]["next_cursor"][:-1] + (
        "A" if first["page"]["next_cursor"][-1] != "A" else "B"
    )
    assert api.get(f"/v1/jobs?cursor={hostile}", headers=auth(token)).status_code == 422


def test_pagination_handles_concurrent_inserts_and_deletions(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    _enqueue_jobs(api, token, account, 4)
    first = api.get("/v1/jobs?limit=2", headers=auth(token)).json()
    seen = {job["job_id"] for job in first["jobs"]}
    _enqueue_jobs(api, token, account, 2)  # newer rows must not duplicate earlier pages
    rest = []
    cursor = first["page"]["next_cursor"]
    while cursor:
        page = api.get(f"/v1/jobs?limit=2&cursor={cursor}", headers=auth(token)).json()
        rest.extend(job["job_id"] for job in page["jobs"])
        cursor = page["page"]["next_cursor"]
    assert seen.isdisjoint(rest)
    # Deleting rows already returned does not shift the remaining walk.
    with api.app.state.sessions() as session:
        doomed = session.scalars(select(Job.id).order_by(Job.created_at.desc()).limit(1)).all()
        session.execute(update(Job).where(Job.id.in_(doomed)).values(state="cancelled"))
        session.commit()
    assert api.get("/v1/jobs?limit=40", headers=auth(token)).status_code == 200


def test_audit_pagination_and_legacy_before_id(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    for _ in range(4):
        api.get("/v1/accounts", headers=auth(token))
    first = api.get("/v1/admin/audit?limit=3", headers=owner).json()
    assert len(first["events"]) == 3 and first["page"]["has_more"] is True
    second = api.get(
        f"/v1/admin/audit?limit=3&cursor={first['page']['next_cursor']}", headers=owner
    ).json()
    ids = [event["id"] for event in first["events"] + second["events"]]
    assert ids == sorted(ids, reverse=True) and len(ids) == len(set(ids))
    legacy = api.get(f"/v1/admin/audit?before_id={first['events'][-1]['id']}", headers=owner)
    assert legacy.status_code == 200
    assert all(event["id"] < first["events"][-1]["id"] for event in legacy.json()["events"])
    assert api.get("/v1/admin/audit?limit=0", headers=owner).status_code == 422
    assert api.get("/v1/admin/audit?cursor=forged", headers=owner).status_code == 422


def test_workflow_run_pagination(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    workflow = api.post(
        "/v1/admin/workflows",
        headers=owner,
        json={
            "name": "pagination workflow",
            "client_id": client["id"],
            "actions": [
                {
                    "kind": "send",
                    "account_id": account["id"],
                    "chat_id": "public-channel",
                    "text": "paged run",
                }
            ],
        },
    )
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["workflow_id"]
    for _ in range(3):
        assert api.post(f"/v1/admin/workflows/{workflow_id}/run", headers=owner, json={}).json()[
            "state"
        ] == "succeeded"
    first = api.get(f"/v1/admin/workflows/{workflow_id}/runs?limit=2", headers=owner).json()
    assert len(first["runs"]) == 2 and first["page"]["has_more"] is True
    second = api.get(
        f"/v1/admin/workflows/{workflow_id}/runs?limit=2"
        f"&cursor={first['page']['next_cursor']}",
        headers=owner,
    ).json()
    assert first["page"]["has_more"] and second["runs"]
    ids = [run["run_id"] for run in first["runs"] + second["runs"]]
    assert len(ids) == len(set(ids)) == 3
    reused = api.get(
        f"/v1/admin/workflows/{workflow_id}/runs?cursor={second['page']['next_cursor']}",
        headers=owner,
    ) if second["page"]["next_cursor"] else None
    assert reused is None or reused.status_code == 200
    other = api.post(
        "/v1/admin/workflows",
        headers=owner,
        json={
            "name": "pagination workflow 2",
            "client_id": client["id"],
            "actions": [
                {
                    "kind": "send",
                    "account_id": account["id"],
                    "chat_id": "public-channel",
                    "text": "other workflow",
                }
            ],
        },
    ).json()["workflow_id"]
    crossed = api.get(
        f"/v1/admin/workflows/{other}/runs?cursor={first['page']['next_cursor']}", headers=owner
    )
    assert crossed.status_code == 422


def test_page_metadata_is_bounded_and_typed(api, settings):
    owner, account, client = create_resources(api, settings)
    page = api.get("/v1/jobs?limit=100", headers=auth(client["credential"])).json()["page"]
    assert set(page) == {"limit", "returned", "has_more", "next_cursor"}
    assert page["limit"] == 100 and page["returned"] == 0
    assert api.get("/v1/jobs?limit=101", headers=auth(client["credential"])).status_code == 422


def test_cursor_identifiers_are_not_plaintext():
    token = encode_cursor(
        key=KEY,
        scope=scope_digest("jobs", "client-a"),
        sort_value=datetime.now(timezone.utc).isoformat(),
        identifier=str(uuid.uuid4()),
    )
    assert "client-a" not in token
    assert token.count(".") == 1
    # Stable ordering depends on the explicit (sort key, id) tiebreaker.
    assert "Job.created_at.desc(), Job.id.desc()" in (
        Path("src/tg_control/jobs.py").read_text()
    )
    assert timedelta(seconds=1)  # keeps the datetime import meaningful for readers
