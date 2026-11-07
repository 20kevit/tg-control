"""Offline-only TDJSON send-update classification; this module performs no I/O.

This is a conservative, synthetic proof of concept, not a native adapter. A future
adapter must supply trusted native client/chat/message identifiers, buffer updates
that precede the initial response, preserve outcomes durably, reauthorize retries,
and obey the compliance gate before any real Telegram use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class SendOutcome:
    # "sent" confirms a TDLib success update, not recipient delivery/read.
    status: Literal["sent", "failed", "unknown"]
    message_id: int | None = None
    scheduled: bool = False
    error_code: int | None = None
    retry_after: float | None = None
    retry_permitted: bool = False


def _int53(value: Any) -> bool:
    return type(value) is int and 0 < abs(value) <= 2**53 - 1


def classify_send_update(
    client_id: int,
    chat_id: int,
    temporary_message_id: int,
    update: dict[str, Any],
) -> SendOutcome | None:
    """Classify an update matching one known send; ignore unmatched/ack updates.

    Only metadata is returned: message contents and error text are never retained.
    An absent result means wait for a matching terminal update (or time out as
    unknown); it is not evidence that a send is safe to repeat.
    """
    if type(client_id) is not int or not 0 < client_id < 2**31:
        raise ValueError("client_id must be a positive int32 value")
    if not _int53(chat_id) or not _int53(temporary_message_id):
        raise ValueError("chat and temporary message IDs must be nonzero int53 values")
    if not isinstance(update, dict):
        raise ValueError("update must be an object")
    if type(update.get("@client_id")) is not int or update["@client_id"] != client_id:
        return None
    kind = update.get("@type")
    if not isinstance(kind, str):
        return None
    if kind == "updateDeleteMessages":
        ids = update.get("message_ids")
        if (
            type(update.get("chat_id")) is int
            and update["chat_id"] == chat_id
            and isinstance(ids, list)
            and any(type(value) is int and value == temporary_message_id for value in ids)
        ):
            # TDLib can delete a failed-to-send message without sending a failure
            # update; deletion alone is not proof that repeating the send is safe.
            return SendOutcome("unknown")
        return None
    if kind not in {"updateMessageSendSucceeded", "updateMessageSendFailed"}:
        return None  # In particular, an acknowledgment is not a successful send.
    message = update.get("message")
    if (
        not isinstance(message, dict)
        or message.get("@type") != "message"
        or type(message.get("chat_id")) is not int
        or message["chat_id"] != chat_id
        or type(update.get("old_message_id")) is not int
        or update["old_message_id"] != temporary_message_id
    ):
        return None
    if kind == "updateMessageSendSucceeded":
        message_id = message.get("id")
        if (
            not _int53(message_id)
            or "sending_state" not in message
            or message["sending_state"] is not None
            or "scheduling_state" not in message
            or (
                message["scheduling_state"] is not None
                and not isinstance(message["scheduling_state"], dict)
            )
        ):
            return SendOutcome("unknown")
        return SendOutcome(
            "sent", message_id=message_id, scheduled=message["scheduling_state"] is not None
        )

    error = update.get("error")
    error_code = (
        error.get("code") if isinstance(error, dict) and error.get("@type") == "error" else None
    )
    if type(error_code) is not int or not -(2**31) <= error_code <= 2**31 - 1:
        error_code = None
    failed = message.get("sending_state")
    retry_after = None
    retry_permitted = False
    if isinstance(failed, dict) and failed.get("@type") == "messageSendingStateFailed":
        seconds = failed.get("retry_after")
        if type(seconds) in (int, float):
            try:
                normalized = float(seconds)
            except OverflowError:
                normalized = math.inf
            if math.isfinite(normalized) and normalized >= 0:
                retry_after = normalized
        unchanged = all(
            failed.get(field) is False
            for field in ("need_another_sender", "need_another_reply_quote", "need_drop_reply")
        )
        stars = failed.get("required_paid_message_star_count")
        retry_permitted = (
            error_code is not None
            and failed.get("can_retry") is True
            and unchanged
            and type(stars) is int
            and stars == 0
            and retry_after is not None
        )
    return SendOutcome(
        "failed", error_code=error_code, retry_after=retry_after, retry_permitted=retry_permitted
    )
