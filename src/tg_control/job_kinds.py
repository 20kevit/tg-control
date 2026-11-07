"""First-party job kinds. Every one executes through Gateway Core and Policy."""

from __future__ import annotations

import uuid
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .auth import Principal
from .core import BatchRejected, Gateway
from .jobs import CancelledNoEffect, JobContext, JobKind, JobOutcome, Jobs
from .policy import ChatId
from .schemas import Batch, Send

MAX_RESULT_FIELDS = 4


class TelegramSendInput(BaseModel):
    """A delayed single send. Only the selected chat and text are persisted."""

    model_config = ConfigDict(extra="forbid")
    account_id: UUID
    chat_id: ChatId
    text: Annotated[str, Field(min_length=1, max_length=16384)]


class TelegramSendJob:
    """Scheduled send that revalidates credential, account and chat authorization."""

    kind = "telegram.send"

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def registration(self) -> JobKind:
        return JobKind(
            name=self.kind,
            input_model=TelegramSendInput,
            handler=self.handle,
            mutating=True,
            authorizer=self.authorize,
        )

    async def authorize(self, principal: Principal, payload: TelegramSendInput) -> None:
        batch = Batch(actions=[self._action(payload)], idempotency_key=uuid.uuid4())
        results = await self.gateway.preauthorize(principal, batch)
        if not results[0]["allowed"]:
            raise PermissionError("denied")

    @staticmethod
    def _action(payload: TelegramSendInput) -> Send:
        return Send(
            account_id=payload.account_id,
            operation="send",
            chat_id=payload.chat_id,
            text=payload.text,
        )

    async def handle(self, context: JobContext) -> JobOutcome:
        if context.client_id is None or context.credential_id is None:
            return JobOutcome(state="failed", error="denied")
        try:
            payload = TelegramSendInput.model_validate(context.input)
        except ValueError:
            return JobOutcome(state="failed", error="invalid_input")
        try:
            context.check_cancelled()
        except CancelledNoEffect:
            return JobOutcome(state="cancelled", error="cancel_requested")
        principal = Principal(context.client_id, context.credential_id)
        batch = Batch(
            actions=[self._action(payload)],
            # The job identity is the idempotency key, so a repeated execution after
            # an unknown outcome cannot silently send a second message.
            idempotency_key=UUID(context.job_id),
        )
        try:
            result = await self.gateway.execute(principal, batch)
        except BatchRejected as exc:
            if exc.status_code == 401:
                return JobOutcome(state="failed", error="credential_revoked")
            if exc.status_code == 409:
                return JobOutcome(state="failed", error="duplicate_request")
            return JobOutcome(state="failed", error="denied")
        item = result["items"][0]
        if item.get("status") != "success":
            error = item.get("error") if isinstance(item.get("error"), str) else "job_failure"
            if item.get("outcome_unknown") is True:
                return JobOutcome(state="outcome_unknown", error=error[:64])
            return JobOutcome(state="failed", error=error[:64])
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        outcome: dict = {}
        if type(data.get("message_id")) is int:
            outcome["message_id"] = data["message_id"]
        return JobOutcome(state="succeeded", result=outcome)


def register_first_party_kinds(jobs: Jobs, gateway: Gateway) -> None:
    """Register the approved first-party job kinds. Unknown kinds stay rejected."""
    jobs.register(TelegramSendJob(gateway).registration())
