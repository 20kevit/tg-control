"""Synthetic schema-shaped fixtures for the offline TDJSON read proof of concept."""

import copy

import pytest

from tg_control.adapters.base import Chat, ChatKind
from tg_control.adapters.tdlib_read_model import classify_chat, passes_text_safety_screen
from tg_control.policy import Scope


def supergroup_chat(is_channel=False):
    return {
        "@type": "chat",
        "id": -100123,
        "type": {"@type": "chatTypeSupergroup", "supergroup_id": 123, "is_channel": is_channel},
        "has_protected_content": False,
        "message_auto_delete_time": 0,
    }


def supergroup(is_channel=False, usernames=None, location=False):
    return {
        "@type": "supergroup",
        "id": 123,
        "is_channel": is_channel,
        "usernames": usernames,
        "has_location": location,
        "is_direct_messages_group": False,
        "is_administered_direct_messages_group": False,
    }


def username_list(*values):
    return {"@type": "usernames", "active_usernames": list(values)}


def direct_chat():
    return {
        "@type": "chat",
        "id": 12345,
        "type": {"@type": "chatTypePrivate", "user_id": 12345},
        "has_protected_content": False,
        "message_auto_delete_time": 0,
    }


def text_message(chat_id=12345):
    return {
        "@type": "message",
        "id": 101,
        "chat_id": chat_id,
        "can_be_saved": True,
        "self_destruct_type": None,
        "self_destruct_in": 0.0,
        "auto_delete_in": 0.0,
        "ephemeral_content": None,
        "ephemeral_message_id": 0,
        "restriction_info": None,
        "sending_state": None,
        "scheduling_state": None,
        "content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": "synthetic untrusted text"},
        },
    }


def test_chat_classification_respects_private_and_public_types():
    assert classify_chat(direct_chat()) == ChatKind.PRIVATE_DIRECT
    basic = {
        "@type": "chat",
        "id": -9,
        "type": {"@type": "chatTypeBasicGroup", "basic_group_id": 9},
    }
    assert classify_chat(basic) == ChatKind.PRIVATE_GROUP
    assert (
        classify_chat(supergroup_chat(), supergroup(usernames=username_list("public_group")))
        == ChatKind.PUBLIC_GROUP
    )
    assert (
        classify_chat(supergroup_chat(True), supergroup(True, username_list("public_channel")))
        == ChatKind.PUBLIC_CHANNEL
    )
    # Official TDLib schema defines location-based supergroups as public too.
    assert classify_chat(supergroup_chat(), supergroup(location=True)) == ChatKind.PUBLIC_GROUP
    assert classify_chat(supergroup_chat(True), supergroup(True)) == ChatKind.PRIVATE_GROUP
    assert classify_chat(supergroup_chat(), supergroup()) == ChatKind.PRIVATE_GROUP
    secret = {
        "@type": "chat",
        "id": -12,
        "type": {"@type": "chatTypeSecret", "secret_chat_id": 12, "user_id": 2},
    }
    assert classify_chat(secret) is None


def test_public_exclusions_and_private_explicit_grants_use_conservative_kind():
    policy = Scope(
        enabled=True,
        allow_types=[ChatKind.PUBLIC_GROUP],
        exclude_types=[ChatKind.PUBLIC_CHANNEL],
        allow_chats=["-100123"],
    )
    private = classify_chat(supergroup_chat(True), supergroup(True))
    public = classify_chat(supergroup_chat(True), supergroup(True, username_list("public_channel")))
    assert private == ChatKind.PRIVATE_GROUP
    assert policy.allows(Chat("-100123", private, "synthetic"))
    assert public == ChatKind.PUBLIC_CHANNEL
    assert not policy.allows(Chat("-100123", public, "synthetic"))
    assert classify_chat(supergroup_chat(True)) is None  # No guessed kind on missing metadata.


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 124},
        {"is_channel": True},
        {"has_location": None},
        {"usernames": {"@type": "usernames", "active_usernames": ["invalid-name"]}},
        {"usernames": {"@type": "usernames", "active_usernames": None}},
        {"is_direct_messages_group": True},
        {"is_administered_direct_messages_group": True},
        {"is_direct_messages_group": None},
    ],
)
def test_malformed_or_ambiguous_supergroup_never_falls_back_to_a_grant(changes):
    info = supergroup()
    info.update(changes)
    assert classify_chat(supergroup_chat(), info) is None
    missing = supergroup()
    del missing["usernames"]
    assert classify_chat(supergroup_chat(), missing) is None
    assert classify_chat(supergroup_chat(True), supergroup(True, location=True)) is None


def test_text_screen_is_structural_only_and_requires_fresh_known_chat():
    chat, message = direct_chat(), text_message()
    assert passes_text_safety_screen(chat, message) is True
    group, info = supergroup_chat(), supergroup(usernames=username_list("public_group"))
    assert not passes_text_safety_screen(group, text_message(group["id"]))
    assert passes_text_safety_screen(group, text_message(group["id"]), info)
    # Re-evaluating a newly protected chat immediately fails; there is no cached decision.
    chat["has_protected_content"] = True
    assert not passes_text_safety_screen(chat, message)


@pytest.mark.parametrize(
    "field,value",
    [
        ("can_be_saved", False),
        ("can_be_saved", 1),
        ("self_destruct_type", {"@type": "messageSelfDestructTypeTimer"}),
        ("self_destruct_in", 2.5),
        ("self_destruct_in", float("nan")),
        ("self_destruct_in", False),
        ("auto_delete_in", 10),
        ("ephemeral_content", {"@type": "ephemeralMessageContent"}),
        ("ephemeral_message_id", 1),
        ("ephemeral_message_id", False),
        ("restriction_info", {"@type": "restrictionInfo"}),
        ("sending_state", {"@type": "messageSendingStatePending"}),
        ("scheduling_state", {"@type": "messageSchedulingStateSendAtDate"}),
        ("chat_id", 99999),
        ("id", 0),
        ("content", {"@type": "messagePhoto", "is_secret": False}),
        ("content", {"@type": "messageText", "text": {"@type": "unexpected"}}),
        ("content", {"@type": "messageText", "text": {"@type": "formattedText"}}),
    ],
)
def test_protected_expiring_malformed_and_unsupported_content_is_rejected(field, value):
    message = text_message()
    message[field] = value
    assert not passes_text_safety_screen(direct_chat(), message)


@pytest.mark.parametrize(
    "field",
    [
        "can_be_saved",
        "self_destruct_type",
        "self_destruct_in",
        "auto_delete_in",
        "ephemeral_content",
        "ephemeral_message_id",
        "restriction_info",
        "sending_state",
        "scheduling_state",
    ],
)
def test_missing_safety_fields_fail_closed(field):
    message = text_message()
    del message[field]
    assert not passes_text_safety_screen(direct_chat(), message)


def test_chat_protection_unknown_secret_and_message_timers_fail_closed():
    chat, message = direct_chat(), text_message()
    for changed in (
        {"has_protected_content": True},
        {"has_protected_content": None},
        {"message_auto_delete_time": 1},
        {"message_auto_delete_time": False},
    ):
        candidate = {**chat, **changed}
        assert not passes_text_safety_screen(candidate, message)
    secret = copy.deepcopy(chat)
    secret["type"] = {"@type": "chatTypeSecret", "secret_chat_id": 1, "user_id": 1}
    assert not passes_text_safety_screen(secret, message)
