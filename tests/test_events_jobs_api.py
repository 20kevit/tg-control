"""HTTP acceptance for durable events and jobs (synthetic data only)."""

import uuid
from datetime import datetime, timezone

from test_api import auth, create_resources, execute, set_policy

from tg_control.adapters.base import ChatKind, Update
from tg_control.events import EventInput


def make_subscription(api, token, account, **overrides):
    body = {
        "account_id": account["id"],
        "name": "inbox",
        "event_types": ["message.new"],
        "chat_ids": ["public-channel"],
        "max_inflight": 10,
    }
    body.update(overrides)
    return api.post("/v1/events/subscriptions", headers=auth(token), json=body)


def ingest_message(api, account, text="synthetic event", message_id=5, upstream=None):
    events = api.app.state.events
    return events.ingest(
        EventInput(
            event_type="message.new",
            account_id=account["id"],
            source="adapter",
            chat_id="public-channel",
            payload={"message_id": message_id, "text": text, "has_media": False},
            upstream_id=upstream or f"public-channel:{message_id}:new",
        )
    )


def test_event_subscription_lifecycle_and_ack_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    assert api.get("/v1/events/subscriptions", headers=owner).status_code == 401
    assert make_subscription(api, token, account).status_code == 201
    subscriptions = api.get("/v1/events/subscriptions", headers=auth(token)).json()[
        "subscriptions"
    ]
    assert len(subscriptions) == 1
    subscription_id = subscriptions[0]["id"]
    assert make_subscription(api, token, account).status_code == 409

    import asyncio

    first = asyncio.run(ingest_message(api, account))
    assert first["status"] == "accepted"
    page = api.post(
        f"/v1/events/subscriptions/{subscription_id}/pull",
        headers=auth(token),
        json={"limit": 10},
    ).json()
    assert len(page["events"]) == 1
    assert page["events"][0]["payload"]["text"] == "synthetic event"
    sequence = page["events"][0]["sequence"]
    ack = api.post(
        f"/v1/events/subscriptions/{subscription_id}/ack",
        headers=auth(token),
        json={"sequence": sequence},
    )
    assert ack.status_code == 200 and ack.json() == {"cursor": sequence}
    backwards = api.post(
        f"/v1/events/subscriptions/{subscription_id}/ack",
        headers=auth(token),
        json={"sequence": 0},
    )
    assert backwards.status_code == 422 and backwards.json()["error"] == "invalid_cursor"
    resumed = api.post(
        f"/v1/events/subscriptions/{subscription_id}/resume", headers=auth(token)
    )
    assert resumed.status_code == 200
    paused = api.post(
        f"/v1/events/subscriptions/{subscription_id}/pause", headers=auth(token)
    ).json()
    assert paused["paused"] is True
    blocked = api.post(
        f"/v1/events/subscriptions/{subscription_id}/pull",
        headers=auth(token),
        json={},
    )
    assert blocked.status_code == 409 and blocked.json()["error"] == "subscription_paused"
    assert (
        api.delete(
            f"/v1/events/subscriptions/{subscription_id}", headers=auth(token)
        ).status_code
        == 204
    )


def test_event_subscription_isolated_between_clients(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    other = api.post("/v1/admin/clients", headers=owner, json={"name": "second client"}).json()
    set_policy(api, owner, account, other)
    sub = make_subscription(api, client["credential"], account).json()
    assert api.get("/v1/events/subscriptions", headers=auth(other["credential"])).json() == {
        "subscriptions": []
    }
    assert (
        api.post(
            f"/v1/events/subscriptions/{sub['id']}/pull",
            headers=auth(other["credential"]),
            json={},
        ).status_code
        == 404
    )
    assert (
        api.delete(
            f"/v1/events/subscriptions/{sub['id']}", headers=auth(other["credential"])
        ).status_code
        == 404
    )


def test_event_subscription_requires_read_scope(api, settings):
    owner, account, client = create_resources(api, settings)
    denied = make_subscription(api, client["credential"], account)
    assert denied.status_code == 403 and denied.json()["error"] == "denied"
    set_policy(
        api,
        owner,
        account,
        client,
        {"read": {"enabled": True, "allow_types": ["public_group"]}},
    )
    rejected = make_subscription(api, client["credential"], account)
    assert rejected.status_code == 422 and rejected.json()["error"] == "unknown_chat"


def test_dead_letter_owner_workflow_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    sub = make_subscription(api, token, account).json()
    import asyncio

    asyncio.run(ingest_message(api, account, upstream="poison-1"))
    page = api.post(
        f"/v1/events/subscriptions/{sub['id']}/pull", headers=auth(token), json={}
    ).json()
    event_id = page["events"][0]["event_id"]
    for _ in range(4):
        nack = api.post(
            f"/v1/events/subscriptions/{sub['id']}/nack",
            headers=auth(token),
            json={"event_id": event_id, "reason": "consumer_error"},
        )
        assert nack.status_code == 200 and nack.json()["status"] == "retry"
    final = api.post(
        f"/v1/events/subscriptions/{sub['id']}/nack",
        headers=auth(token),
        json={"event_id": event_id, "reason": "consumer_error"},
    ).json()
    assert final["status"] == "dead_letter"
    assert api.get("/v1/admin/events/dead-letters", headers=auth(token)).status_code == 401
    letters = api.get("/v1/admin/events/dead-letters", headers=owner).json()["dead_letters"]
    assert len(letters) == 1 and letters[0]["reason"] == "consumer_error"
    replay = api.post(
        f"/v1/admin/events/dead-letters/{letters[0]['id']}/replay", headers=owner
    )
    assert replay.status_code == 200
    assert api.get("/v1/admin/events/dead-letters", headers=owner).json()["dead_letters"] == []


def test_adapter_updates_reach_subscribers_after_ingestion(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    sub = make_subscription(api, token, account).json()
    adapter = api.app.state.gateway.adapter
    adapter.inject_update(
        Update(
            event_type="message.new",
            account_id=account["id"],
            chat_id="public-channel",
            payload={"message_id": 11, "text": "adapter event", "has_media": False},
            upstream_id="public-channel:11:new",
        )
    )
    import asyncio

    counts = asyncio.run(api.app.state.events.ingest_updates(account["id"]))
    assert counts["accepted"] == 1
    page = api.post(
        f"/v1/events/subscriptions/{sub['id']}/pull", headers=auth(token), json={}
    ).json()
    assert [event["payload"]["text"] for event in page["events"]] == ["adapter event"]


def test_job_lifecycle_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    body = {
        "kind": "telegram.send",
        "input": {"account_id": account["id"], "chat_id": "public-channel", "text": "scheduled"},
        "schedule": {"kind": "once", "run_at": datetime.now(timezone.utc).isoformat()},
    }
    created = api.post("/v1/jobs", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    assert api.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()["state"] in {
        "queued",
        "scheduled",
    }
    tick = api.post("/v1/admin/jobs/tick", headers=owner)
    assert tick.status_code == 200
    inspected = api.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert inspected["state"] == "succeeded"
    assert inspected["runs"][0]["state"] == "succeeded"
    history = execute(api, token, account, "history", "public-channel").json()
    texts = [message["text"] for message in history["items"][0]["data"]["messages"]]
    assert "scheduled" in texts
    # Clients cannot reach owner management or another client's jobs.
    assert api.get("/v1/admin/jobs", headers=auth(token)).status_code == 401
    other = api.post("/v1/admin/clients", headers=owner, json={"name": "other job client"}).json()
    assert api.get(f"/v1/jobs/{job_id}", headers=auth(other["credential"])).status_code == 404
    empty_page = api.get("/v1/jobs", headers=auth(other["credential"])).json()
    assert empty_page["jobs"] == [] and empty_page["page"]["has_more"] is False


def test_job_validation_and_authorization_errors(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    base = {
        "kind": "telegram.send",
        "input": {"account_id": account["id"], "chat_id": "public-channel", "text": "x"},
        "schedule": {"kind": "once", "run_at": datetime.now(timezone.utc).isoformat()},
    }
    denied = api.post("/v1/jobs", headers=auth(token), json=base)
    assert denied.status_code == 403 and denied.json()["error"] == "denied"
    set_policy(api, owner, account, client)
    unknown = api.post("/v1/jobs", headers=auth(token), json={**base, "kind": "unknown.kind"})
    assert unknown.status_code == 422
    invalid = api.post(
        "/v1/jobs",
        headers=auth(token),
        json={**base, "input": {"account_id": account["id"], "chat_id": "x", "text": "y", "z": 1}},
    )
    assert invalid.status_code == 422
    assert api.post("/v1/jobs", headers=owner, json=base).status_code == 401
    assert api.get("/v1/jobs", headers=owner).status_code == 401


def test_job_cancel_and_pause_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    body = {
        "kind": "telegram.send",
        "input": {"account_id": account["id"], "chat_id": "public-channel", "text": "cancel me"},
        "schedule": {"kind": "once", "run_at": "2099-01-01T00:00:00+00:00"},
    }
    created = api.post("/v1/jobs", headers=auth(token), json=body).json()
    job_id = created["job_id"]
    assert api.post(f"/v1/jobs/{job_id}/pause", headers=auth(token)).status_code == 200
    assert api.post(f"/v1/jobs/{job_id}/resume", headers=auth(token)).status_code == 200
    cancelled = api.post(f"/v1/jobs/{job_id}/cancel", headers=auth(token))
    assert cancelled.json()["decision"] == "cancelled"
    assert api.post(f"/v1/jobs/{job_id}/cancel", headers=auth(token)).status_code == 409
    owner_view = api.get(f"/v1/admin/jobs/{job_id}", headers=owner).json()
    assert owner_view["state"] == "cancelled"


def test_event_ingestion_rejects_cross_account_and_unknown_chat(api, settings):
    owner, account, client = create_resources(api, settings)
    events = api.app.state.events
    import asyncio

    with pytest_raises_event_error("unknown_chat"):
        asyncio.run(
            events.ingest(
                EventInput(
                    event_type="message.new",
                    account_id=account["id"],
                    source="adapter",
                    chat_id="not-a-chat",
                    payload={"message_id": 1, "has_media": False},
                )
            )
        )
    with pytest_raises_event_error("unknown_account"):
        asyncio.run(
            events.ingest(
                EventInput(
                    event_type="message.new",
                    account_id=str(uuid.uuid4()),
                    source="adapter",
                    chat_id="public-channel",
                    payload={"message_id": 1, "has_media": False},
                )
            )
        )


class pytest_raises_event_error:
    def __init__(self, code):
        self.code = code

    def __enter__(self):
        from tg_control.events import EventError

        self._expected = EventError
        return self

    def __exit__(self, exc_type, exc, _tb):
        assert exc_type is not None, "expected an event error"
        assert isinstance(exc, self._expected)
        assert exc.code == self.code
        return True


def test_settings_include_event_retention(api, settings):
    owner, account, client = create_resources(api, settings)
    settings_body = api.get("/v1/admin/settings", headers=owner).json()
    assert settings_body["event_retention_days"] == 30
    updated = api.put(
        "/v1/admin/settings",
        headers=owner,
        json={**settings_body, "event_retention_days": 7},
    )
    assert updated.json()["event_retention_days"] == 7
    pruned = api.post("/v1/admin/maintenance/prune-events", headers=owner)
    assert pruned.status_code == 200
    assert "deleted" in pruned.json()


def test_public_group_broad_grant_still_excluded_from_events(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(
        api,
        owner,
        account,
        client,
        {
            "read": {
                "enabled": True,
                "allow_types": ["public_channel"],
                "exclude_chats": ["public-channel"],
            },
            "write": {"enabled": True, "allow_chats": ["public-channel"]},
        },
    )
    token = client["credential"]
    sub = make_subscription(api, token, account, event_types=[], chat_ids=[])
    assert sub.status_code == 201
    import asyncio

    asyncio.run(ingest_message(api, account, upstream="excluded-1"))
    page = api.post(
        f"/v1/events/subscriptions/{sub.json()['id']}/pull", headers=auth(token), json={}
    ).json()
    assert page["events"] == []
    assert ChatKind.PUBLIC_CHANNEL.value == "public_channel"
