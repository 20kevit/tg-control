"""Provider-independent AI task contract conformance tests (synthetic only)."""

import asyncio

import pytest
from sqlalchemy import select

from tg_control.ai import (
    AIError,
    AIService,
    ConformanceInput,
    ConformanceOutput,
    FakeProvider,
    ProviderCapabilities,
    ProviderError,
    TaskSpec,
    conformance_task,
)
from tg_control.auth import Principal, rotate_credential
from tg_control.db import AuditEvent, Client, Credential


class Env:
    def __init__(self, database):
        self.database = database
        self.provider = FakeProvider()
        self.service = AIService(database, sleep=self._fast_sleep)
        self.service.register_provider(self.provider)
        self.service.register_task(conformance_task())
        with database() as session:
            self.client = Client(name="ai client")
            session.add(self.client)
            session.commit()
            rotate_credential(session, self.client.id)
            session.commit()
            self.principal = Principal(
                client_id=self.client.id,
                credential_id=session.scalar(
                    select(Credential.id).where(
                        Credential.client_id == self.client.id,
                        Credential.revoked.is_(False),
                    )
                ),
            )

    @staticmethod
    async def _fast_sleep(_delay):
        return None

    async def invoke(self, payload=None, **kwargs):
        return await self.service.invoke(
            self.principal,
            "conformance.echo",
            payload if payload is not None else {"text": "exact caller input"},
            provider_name=kwargs.pop("provider_name", "synthetic-local"),
            **kwargs,
        )


@pytest.fixture
def env(database):
    return Env(database)


async def test_explicit_invocation_and_exact_input_only(env):
    assert env.provider.calls == []  # nothing runs without an explicit invocation
    result = await env.invoke()
    assert result["output"] == {"text": "exact caller input"}
    assert result["provider"] == "synthetic-local"
    assert result["usage"]["attempts"] == 1
    assert len(env.provider.calls) == 1
    assert env.provider.calls[0].input == {
        "text": "exact caller input",
        "source": "caller_supplied",
    }
    with env.database() as session:
        rows = session.scalars(select(AuditEvent).where(AuditEvent.event == "ai_task")).all()
    assert rows and rows[0].decision == "success"
    blob = " ".join(f"{row.event}|{row.decision}|{row.error_code}" for row in rows)
    assert "exact caller input" not in blob


async def test_telegram_references_and_secrets_are_rejected_before_provider(env):
    for payload in (
        {"text": "x", "chat_id": "public-channel"},
        {"text": "x", "account_id": "00000000-0000-0000-0000-000000000000"},
        {"text": "x", "messages": [{"id": 1}]},
        {"text": "x", "api_key": "secret"},
    ):
        with pytest.raises(AIError) as exc:
            await env.invoke(payload)
        assert exc.value.code in {"compliance_blocked", "invalid_input"}
    assert env.provider.calls == []


async def test_unknown_task_provider_model_and_capability(env):
    with pytest.raises(AIError) as task:
        await env.service.invoke(
            env.principal, "unknown.task", {"text": "x"}, provider_name="synthetic-local"
        )
    assert task.value.code == "unknown_task"
    with pytest.raises(AIError) as provider:
        await env.invoke(provider_name="unknown-provider")
    assert provider.value.code == "unknown_provider"
    with pytest.raises(AIError) as model:
        await env.invoke(model="other-model")
    assert model.value.code == "unknown_model"
    limited = FakeProvider(name="limited", task_kinds=("other.kind",))
    env.service.register_provider(limited)
    with pytest.raises(AIError) as unsupported:
        await env.invoke(provider_name="limited")
    assert unsupported.value.code == "unsupported_task"
    assert env.provider.calls == [] and limited.calls == []


async def test_input_and_cost_budgets(env):
    with pytest.raises(AIError) as too_large:
        await env.invoke({"text": "x" * 5000})
    assert too_large.value.code == "input_too_large"
    priced = FakeProvider(name="priced", input_price=10.0, output_price=10.0)
    env.service.register_provider(priced)
    with pytest.raises(AIError) as budget:
        await env.invoke(provider_name="priced")
    assert budget.value.code == "budget_exceeded"
    assert priced.calls == []


async def test_timeout_cancellation_and_retry_semantics(env):
    slow = FakeProvider(name="slow", delay=0.2)
    env.service.register_provider(slow)
    original = asyncio.wait_for

    async def short_timeout(awaitable, timeout):
        return await original(awaitable, timeout=0.01)

    asyncio.wait_for = short_timeout
    try:
        with pytest.raises(AIError) as timeout:
            await env.invoke(provider_name="slow")
    finally:
        asyncio.wait_for = original
    assert timeout.value.code == "timeout" and timeout.value.status_code == 504

    retrying = FakeProvider(name="retrying", fail_first=1)
    env.service.register_provider(retrying)
    result = await env.invoke(provider_name="retrying")
    assert result["usage"]["attempts"] == 2
    assert len(retrying.calls) == 2

    failing = FakeProvider(
        name="failing", fail_first=5, failure=ProviderError("down", retryable=False)
    )
    env.service.register_provider(failing)
    with pytest.raises(AIError) as failed:
        await env.invoke(provider_name="failing")
    assert failed.value.code == "provider_error"
    assert len(failing.calls) == 1  # non-retryable providers are not retried

    applied = FakeProvider(
        name="applied", fail_first=1, failure=ProviderError("weird", retryable=True, applied=True)
    )
    env.service.register_provider(applied)
    with pytest.raises(AIError) as applied_error:
        await env.invoke(provider_name="applied")
    assert applied_error.value.code == "provider_error"
    assert len(applied.calls) == 1


async def test_output_validation_rejects_malformed_provider_output(env):
    class BadOutputProvider(FakeProvider):
        def capabilities(self):
            return ProviderCapabilities(
                name="bad-output",
                models=("synthetic-model-1",),
                task_kinds=("conformance.echo",),
                max_input_chars=1000,
                max_output_tokens=16,
                price_per_1k_input=0.0,
                price_per_1k_output=0.0,
            )

        async def run(self, request):
            return __import__(
                "tg_control.ai", fromlist=["ProviderResult"]
            ).ProviderResult(output={"wrong": "shape"}, input_tokens=1, output_tokens=1, model="m")

    env.service.register_provider(BadOutputProvider(name="bad-output"))
    with pytest.raises(AIError) as invalid:
        await env.invoke(provider_name="bad-output")
    assert invalid.value.code == "invalid_output"


async def test_revoked_credentials_and_registration_rules(env):
    from sqlalchemy import update

    with env.database() as session:
        session.execute(
            update(Credential)
            .where(Credential.id == env.principal.credential_id)
            .values(revoked=True)
        )
        session.commit()
    with pytest.raises(AIError) as revoked:
        await env.invoke()
    assert revoked.value.code == "credential_revoked"
    with pytest.raises(ValueError):
        env.service.register_provider(FakeProvider())
    with pytest.raises(ValueError):
        env.service.register_task(conformance_task())
    with pytest.raises(ValueError):
        env.service.register_task(
            TaskSpec(
                name="background.task",
                version=1,
                task_kind="background.task",
                description="not approved",
                input_model=ConformanceInput,
                output_model=ConformanceOutput,
                invocation=("event",),
            )
        )
    with pytest.raises(ValueError):
        ProviderCapabilities(
            name="x",
            models=(),
            task_kinds=("a",),
            max_input_chars=10,
            max_output_tokens=10,
            price_per_1k_input=0,
            price_per_1k_output=0,
        )


async def test_catalog_has_no_approved_utility_by_default(database):
    service = AIService(database)
    assert service.catalog() == []
    assert service.providers() == []
    with pytest.raises(AIError):
        await service.invoke(
            Principal(client_id="x", credential_id="y"),
            "summarize",
            {"text": "x"},
            provider_name="none",
        )


async def test_audit_includes_no_input_or_output(env):
    await env.invoke({"text": "sensitive synthetic payload"})
    with env.database() as session:
        rows = session.scalars(select(AuditEvent)).all()
    blob = " ".join(
        f"{row.event}|{row.decision}|{row.error_code}|{row.chat_id}" for row in rows
    )
    assert "sensitive synthetic payload" not in blob
