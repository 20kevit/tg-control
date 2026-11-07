"""Plugin manifest, capability grant and containment tests (synthetic, first-party)."""

import asyncio
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.ai import FakeProvider  # noqa: F401  (kept for symmetry with AI tests)
from tg_control.auth import Principal, rotate_credential
from tg_control.core import Gateway
from tg_control.db import Account, AuditEvent, Client, Grant
from tg_control.plugins import (
    ExtensionPoint,
    PluginBudgets,
    PluginCapability,
    PluginError,
    PluginManifest,
    PluginRegistry,
    PluginState,
)
from tg_control.policy import Scope
from tg_control.schemas import Send


class EchoModule:
    """First-party module shape: identity plus a callable entry point."""

    def __init__(self, plugin_id="firstparty.echo"):
        self.plugin_id = plugin_id

    def run(self):  # pragma: no cover - bound module contract only
        return "ok"


class Env:
    def __init__(self, database):
        self.database = database
        self.adapter = SyntheticAdapter()
        self.gateway = Gateway(database, self.adapter, sleep=lambda _d: asyncio.sleep(0))
        self.registry = PluginRegistry(database, self.gateway)
        with database() as session:
            self.account = Account(alias="plugin account", phone_encrypted="synthetic")
            self.client = Client(name="plugin client")
            session.add_all([self.account, self.client])
            session.commit()
            rotate_credential(session, self.client.id)
            session.commit()
            self.principal = Principal(
                client_id=self.client.id,
                credential_id=session.scalar(
                    select(__import__("tg_control.db", fromlist=["Credential"]).Credential.id)
                    .where(
                        __import__("tg_control.db", fromlist=["Credential"]).Credential.client_id
                        == self.client.id
                    )
                ),
            )
        self.set_policy()

    def set_policy(self, enabled=True):
        scope = Scope(enabled=enabled, allow_types=["public_channel"])
        with self.database() as session:
            existing = session.get(Grant, (self.client.id, self.account.id))
            values = {
                "read_scope": scope.model_dump(mode="json"),
                "write_scope": scope.model_dump(mode="json"),
                "admin_scope": Scope().model_dump(mode="json"),
            }
            if existing is None:
                session.add(Grant(client_id=self.client.id, account_id=self.account.id, **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    def manifest(self, **overrides):
        body = {
            "plugin_id": "firstparty.echo",
            "display_name": "Echo plugin",
            "publisher": "tg-control first party",
            "version": "1.0.0",
            "sdk_range": ">=1.0,<2.0",
            "entry_point": "tg_control_firstparty.echo:run",
            "description": "synthetic fixture plugin",
            "extension_points": [ExtensionPoint.MEDIA_PROCESSOR],
            "capabilities": [PluginCapability.MEDIA, PluginCapability.WRITE, PluginCapability.READ],
            "client_id": self.client.id,
            "account_ids": [self.account.id],
        }
        body.update(overrides)
        return PluginManifest(**body)

    def installed_enabled(self, **overrides):
        manifest = self.manifest(**overrides)
        self.registry.install(manifest)
        self.registry.bind(manifest.plugin_id, EchoModule())
        self.registry.grant(manifest.plugin_id, manifest.capabilities)
        return self.registry.enable(manifest.plugin_id)

    async def send(self, text="plugin effect", chat="public-channel"):
        return await self.registry.runtime("firstparty.echo").execute(
            __import__("tg_control.schemas", fromlist=["Batch"]).Batch(
                actions=[
                    Send(
                        operation="send",
                        account_id=self.account.id,
                        chat_id=chat,
                        text=text,
                    )
                ],
                idempotency_key=uuid.uuid4(),
            )
        )

    async def history(self):
        return await self.registry.runtime("firstparty.echo").history("public-channel", 50)


@pytest.fixture
def env(database):
    return Env(database)


def test_manifest_rejects_unsafe_or_incoherent_declarations(env):
    with pytest.raises(ValidationError):
        env.manifest(plugin_id="Bad/Path")
    with pytest.raises(ValidationError):
        env.manifest(entry_point="os.system")
    with pytest.raises(ValidationError):
        env.manifest(version="1.0")
    with pytest.raises(ValidationError):
        env.manifest(sdk_range=">=3.0")
    with pytest.raises(ValidationError):
        env.manifest(capabilities=[PluginCapability.READ, "admin"])
    with pytest.raises(ValidationError):
        env.manifest(config={"api_token": "real-secret-value"})
    with pytest.raises(ValidationError):
        env.manifest(config={"nested": {"a": 1}})
    with pytest.raises(ValidationError):
        PluginManifest(
            **{
                **env.manifest().model_dump(),
                "config": {"api_token": "x" * 600},
            }
        )
    with pytest.raises(ValidationError):
        env.manifest(extension_points=[ExtensionPoint.EVENT_CONSUMER])
    with pytest.raises(ValidationError):
        env.manifest(client_id=None)
    reference = env.manifest(config={"api_key": "$secret:provider-key"})
    assert reference.config["api_key"] == "$secret:provider-key"
    assert "provider-key" not in str(env.registry.list())


def test_install_requires_explicit_grants_and_binding(env):
    manifest = env.manifest()
    installed = env.registry.install(manifest)
    assert installed["state"] == PluginState.INSTALLED.value
    assert installed["granted_capabilities"] == []
    with pytest.raises(PluginError) as not_bound:
        env.registry.enable(manifest.plugin_id)
    assert not_bound.value.code == "module_not_bound"
    env.registry.bind(manifest.plugin_id, EchoModule())
    with pytest.raises(PluginError) as ungranted:
        env.registry.enable(manifest.plugin_id)
    assert ungranted.value.code == "capability_not_granted"
    with pytest.raises(PluginError) as overgrant:
        env.registry.grant(manifest.plugin_id, [PluginCapability.AI_TASK])
    assert overgrant.value.code == "capability_not_declared"
    env.registry.grant(manifest.plugin_id, [PluginCapability.WRITE])
    with pytest.raises(PluginError):
        env.registry.enable(manifest.plugin_id)
    env.registry.grant(manifest.plugin_id, manifest.capabilities)
    assert env.registry.enable(manifest.plugin_id)["state"] == PluginState.ENABLED.value


def test_plugin_cannot_use_capabilities_or_principal_it_was_not_granted(env):
    manifest = env.manifest(
        capabilities=[PluginCapability.READ, PluginCapability.WRITE],
        extension_points=[ExtensionPoint.WORKER],
    )
    env.registry.install(manifest)
    env.registry.bind(manifest.plugin_id, EchoModule())
    env.registry.grant(manifest.plugin_id, [PluginCapability.READ, PluginCapability.WRITE])
    env.registry.enable(manifest.plugin_id)
    with pytest.raises(PluginError) as mismatch:
        asyncio.run(
            env.registry.runtime(manifest.plugin_id).execute(
                __import__("tg_control.schemas", fromlist=["Batch"]).Batch(
                    actions=[
                        Send(
                            operation="send",
                            account_id=env.account.id,
                            chat_id="public-channel",
                            text="stolen principal",
                        )
                    ],
                    idempotency_key=uuid.uuid4(),
                ),
                principal=Principal(client_id="another-client", credential_id="x"),
            )
        )
    assert mismatch.value.code == "principal_mismatch"
    env.registry.disable(manifest.plugin_id)
    with pytest.raises(PluginError) as disabled:
        asyncio.run(env.send())
    assert disabled.value.code == "plugin_not_enabled"


async def test_enabled_plugin_acts_under_delegated_client_policy(env):
    env.installed_enabled()
    result = await env.send()
    assert result["items"][0]["status"] == "success"
    history = await env.history()
    assert "plugin effect" in [message["text"] for message in history["messages"]]
    env.set_policy(enabled=False)
    with pytest.raises(PluginError) as denied:
        await env.send("denied after revocation")
    assert denied.value.code == "denied"
    env.set_policy()
    history = await env.history()
    assert "denied after revocation" not in [message["text"] for message in history["messages"]]


async def test_rate_limit_timeout_and_audit_containment(env):
    env.installed_enabled(
        budgets=PluginBudgets(max_actions_per_minute=2, action_timeout_seconds=0.1)
    )
    await env.send("first")
    await env.send("second")
    with pytest.raises(PluginError) as limited:
        await env.send("third")
    assert limited.value.code == "plugin_rate_limited"

    slow = PluginRegistry(
        env.database, env.gateway, clock=lambda: 1000.0
    )

    class SlowGateway:
        async def execute(self, *_args, **_kwargs):
            await asyncio.sleep(0.2)

    slow.gateway = SlowGateway()
    slow.install(
        env.manifest(
            plugin_id="firstparty.slow",
            budgets=PluginBudgets(max_actions_per_minute=10, action_timeout_seconds=0.1),
        )
    )
    slow.bind("firstparty.slow", EchoModule("firstparty.slow"))
    slow.grant("firstparty.slow", env.manifest().capabilities)
    slow.enable("firstparty.slow")
    batch = __import__("tg_control.schemas", fromlist=["Batch"]).Batch(
        actions=[
            Send(
                operation="send",
                account_id=env.account.id,
                chat_id="public-channel",
                text="never sent",
            )
        ],
        idempotency_key=uuid.uuid4(),
    )
    with pytest.raises(PluginError) as timed_out:
        await slow.runtime("firstparty.slow").execute(batch)
    assert timed_out.value.code == "plugin_timeout"
    with env.database() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event.like("plugin%"))
        ).all()
    assert any(event.decision == "rate_limited" for event in events)
    assert all("first" != (event.error_code or "") for event in events)


async def test_repeated_failures_quarantine_without_affecting_core(env):
    env.installed_enabled()
    registry = env.registry

    async def boom(*_args, **_kwargs):
        raise RuntimeError("plugin bug with secret payload")

    registry.gateway = type("BadGateway", (), {"execute": boom})()
    for _ in range(3):
        with pytest.raises(PluginError) as failure:
            await env.send()
        assert failure.value.code == "plugin_error"
    assert registry.get("firstparty.echo")["state"] == PluginState.QUARANTINED.value
    with pytest.raises(PluginError) as quarantined:
        await env.send()
    assert quarantined.value.code == "plugin_not_enabled"

    original = Gateway(env.database, env.adapter, sleep=lambda _d: asyncio.sleep(0))
    result = await original.execute(
        env.principal,
        __import__("tg_control.schemas", fromlist=["Batch"]).Batch(
            actions=[
                Send(
                    operation="send",
                    account_id=env.account.id,
                    chat_id="public-channel",
                    text="core still works",
                )
            ],
            idempotency_key=uuid.uuid4(),
        ),
    )
    assert result["items"][0]["status"] == "success"
    with env.database() as session:
        blob = " ".join(
            (event.error_code or "") + (event.event or "")
            for event in session.scalars(select(AuditEvent)).all()
        )
    assert "secret payload" not in blob


async def test_upgrade_requires_new_version_and_rebinding(env):
    env.installed_enabled()
    with pytest.raises(PluginError) as duplicate:
        env.registry.install(env.manifest())
    assert duplicate.value.code == "duplicate_plugin"
    with pytest.raises(PluginError) as downgrade:
        env.registry.install(env.manifest(version="0.9.0"))
    assert downgrade.value.code == "downgrade_rejected"
    upgraded = env.registry.install(
        env.manifest(
            version="1.1.0",
            capabilities=[*env.manifest().capabilities, PluginCapability.COMMAND],
            extension_points=[
                ExtensionPoint.MEDIA_PROCESSOR,
                ExtensionPoint.COMMAND,
            ],
        )
    )
    assert upgraded["state"] == PluginState.INSTALLED.value
    assert upgraded["granted_capabilities"] == ["media", "read", "write"]
    with pytest.raises(PluginError) as unbound:
        env.registry.enable("firstparty.echo")
    assert unbound.value.code == "module_not_bound"
    env.registry.bind("firstparty.echo", EchoModule())
    with pytest.raises(PluginError) as new_cap:
        env.registry.enable("firstparty.echo")
    assert new_cap.value.code == "capability_not_granted"
    env.registry.grant("firstparty.echo", [PluginCapability.COMMAND])
    assert env.registry.enable("firstparty.echo")["state"] == "enabled"


async def test_concurrent_plugin_calls_are_bounded(env):
    """Plugin execution is bounded in-process and refuses rather than queueing."""
    from tg_control.schemas import Batch

    manifest = env.manifest(
        capabilities=[PluginCapability.READ, PluginCapability.WRITE],
        extension_points=[ExtensionPoint.WORKER],
    )
    env.installed_enabled(
        capabilities=manifest.capabilities, extension_points=manifest.extension_points
    )
    with pytest.raises(ValueError):
        PluginRegistry(env.database, env.gateway, max_inflight=0)

    env.registry.max_inflight = 1
    release = asyncio.Event()
    started = asyncio.Event()
    original = env.gateway.execute

    async def slow_execute(principal, batch):
        started.set()
        await release.wait()
        return await original(principal, batch)

    env.gateway.execute = slow_execute
    runtime = env.registry.runtime("firstparty.echo")
    batch = Batch(
        actions=[
            Send(
                operation="send",
                account_id=env.account.id,
                chat_id="public-channel",
                text="synthetic plugin concurrency",
            )
        ],
        idempotency_key=uuid.uuid4(),
    )
    first = asyncio.create_task(runtime.execute(batch))
    await started.wait()
    with pytest.raises(PluginError) as busy:
        await runtime.execute(batch)
    assert busy.value.code == "plugin_busy" and busy.value.status_code == 429
    release.set()
    result = await first
    assert result["items"][0]["status"] == "success"
    assert env.registry._inflight == 0
    env.gateway.execute = original
