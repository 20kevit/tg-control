"""Synthetic TDJSON fixtures only; none of these tests use a network or credentials."""

import math
import uuid

import pytest

from tg_control.adapters.tdlib_outcomes import SendOutcome, classify_send_update
from tg_control.adapters.tdlib_poc import OfflineTdJsonRouter

CLIENT, CHAT, TEMPORARY, FINAL = 7, -1234, -5678, 9012


def success(**changes):
    result = {
        "@client_id": CLIENT,
        "@type": "updateMessageSendSucceeded",
        "old_message_id": TEMPORARY,
        "message": {
            "@type": "message",
            "chat_id": CHAT,
            "id": FINAL,
            "sending_state": None,
            "scheduling_state": None,
            "content": {"text": "synthetic untrusted content"},
        },
    }
    result.update(changes)
    return result


def failure(**changes):
    state = {
        "@type": "messageSendingStateFailed",
        "can_retry": True,
        "need_another_sender": False,
        "need_another_reply_quote": False,
        "need_drop_reply": False,
        "required_paid_message_star_count": 0,
        "retry_after": 7.25,
    }
    state.update(changes)
    return {
        "@client_id": CLIENT,
        "@type": "updateMessageSendFailed",
        "old_message_id": TEMPORARY,
        "message": {"@type": "message", "chat_id": CHAT, "sending_state": state},
        "error": {"@type": "error", "code": 429, "message": "synthetic untrusted error text"},
    }


def classify(update):
    return classify_send_update(CLIENT, CHAT, TEMPORARY, update)


def test_ack_is_not_success_and_success_is_correlated_without_content():
    ack = {
        "@client_id": CLIENT,
        "@type": "updateMessageSendAcknowledged",
        "chat_id": CHAT,
        "message_id": TEMPORARY,
    }
    assert classify(ack) is None
    outcome = classify(success())
    assert outcome == SendOutcome("sent", message_id=FINAL)
    assert "synthetic" not in repr(outcome)
    scheduled = success()
    scheduled["message"]["scheduling_state"] = {"@type": "messageSchedulingStateSendAtDate"}
    assert classify(scheduled) == SendOutcome("sent", message_id=FINAL, scheduled=True)


def test_never_confuse_accounts_chats_or_temporary_message_ids():
    for update in (
        success(**{"@client_id": CLIENT + 1}),
        success(**{"@client_id": True}),
        success(old_message_id=TEMPORARY - 1),
        success(message={"chat_id": CHAT - 1, "id": FINAL}),
        success(message={"chat_id": True, "id": FINAL}),
        {"@client_id": CLIENT, "@type": ["unexpected"]},
    ):
        assert classify(update) is None
    assert classify(
        success(message={"@type": "message", "chat_id": CHAT, "id": FINAL})
    ) == SendOutcome("unknown")
    assert classify(
        success(
            message={
                "@type": "message",
                "chat_id": CHAT,
                "id": FINAL,
                "sending_state": None,
                "scheduling_state": "unexpected",
            }
        )
    ) == SendOutcome("unknown")


def test_failure_exposes_only_metadata_and_exact_server_delay():
    outcome = classify(failure())
    assert outcome == SendOutcome("failed", error_code=429, retry_after=7.25, retry_permitted=True)
    assert "synthetic" not in repr(outcome)
    assert classify(failure(retry_after=3600.0)).retry_after == 3600.0
    malformed = failure()
    malformed["error"] = {"@type": "unexpected", "code": 429}
    assert classify(malformed).retry_permitted is False
    assert classify(malformed).error_code is None


@pytest.mark.parametrize(
    "changes",
    [
        {"can_retry": False},
        {"can_retry": 1},
        {"need_another_sender": True},
        {"need_another_reply_quote": True},
        {"need_drop_reply": True},
        {"required_paid_message_star_count": 1},
        {"required_paid_message_star_count": False},
        {"retry_after": -1},
        {"retry_after": math.inf},
        {"retry_after": math.nan},
        {"retry_after": True},
        {"retry_after": "4"},
        {"retry_after": 10**1000},
    ],
)
def test_retry_is_not_authorized_for_changed_payload_payment_or_bad_delay(changes):
    result = classify(failure(**changes))
    assert result.status == "failed"
    assert result.retry_permitted is False


def test_irrecoverable_deletion_or_malformed_success_is_unknown_not_retryable():
    deleted = {
        "@client_id": CLIENT,
        "@type": "updateDeleteMessages",
        "chat_id": CHAT,
        "message_ids": [TEMPORARY],
    }
    assert classify(deleted) == SendOutcome("unknown")
    assert classify({**deleted, "@client_id": CLIENT + 1}) is None
    assert classify({**deleted, "message_ids": [True]}) is None
    assert classify(success(message={"@type": "message", "chat_id": CHAT, "id": 0})) == SendOutcome(
        "unknown"
    )
    failed = failure()
    failed["message"]["sending_state"] = None
    assert classify(failed) == SendOutcome("failed", error_code=429)


@pytest.mark.parametrize(
    "client,chat,message",
    [
        (0, CHAT, TEMPORARY),
        (True, CHAT, TEMPORARY),
        (CLIENT, 0, TEMPORARY),
        (CLIENT, CHAT, 2**53),
        (CLIENT, False, TEMPORARY),
    ],
)
def test_identifiers_are_validated(client, chat, message):
    with pytest.raises(ValueError):
        classify_send_update(client, chat, message, success())


@pytest.mark.parametrize(
    "state,expected",
    [
        ("authorizationStateWaitTdlibParameters", "initializing"),
        ("authorizationStateReady", "ready"),
        ("authorizationStateLoggingOut", "disconnected"),
        ("authorizationStateClosing", "disconnected"),
        ("authorizationStateClosed", "disconnected"),
        ("authorizationStateWaitCode", "reauth_required"),
        ("authorizationStateWaitEmailAddress", "reauth_required"),
        ("authorizationStateWaitEmailCode", "reauth_required"),
        ("authorizationStateWaitOtherDeviceConfirmation", "reauth_required"),
        ("authorizationStateWaitRegistration", "reauth_required"),
        ("authorizationStateWaitPassword", "reauth_required"),
        ("authorizationStateWaitPhoneNumber", "reauth_required"),
        ("authorizationStateWaitPremiumPurchase", "reauth_required"),
        ("authorizationStateNotYetKnown", "reauth_required"),
        (None, "reauth_required"),
    ],
)
def test_offline_authorization_states_fail_closed_without_retaining_details(state, expected):
    router = OfflineTdJsonRouter()
    account = str(uuid.uuid4())
    router.register(CLIENT, account)
    router.receive(
        CLIENT,
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {
                "@type": state,
                "password_hint": "synthetic-secret",
                "link": "synthetic-link",
            },
        },
    )
    assert router.states[account] == expected
    assert "synthetic" not in repr(router.states)


def test_offline_router_rejects_invalid_or_ambiguous_identifiers():
    router = OfflineTdJsonRouter()
    account = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    for client, identifier in [(0, account), (True, account), (1, account.upper())]:
        with pytest.raises(ValueError):
            router.register(client, identifier)
    router.register(1, account)
    with pytest.raises(ValueError):
        router.request(True, "getChat")
    with pytest.raises(ValueError):
        router.receive(True, {"@type": "ok"})
