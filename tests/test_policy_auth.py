from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from tg_control import audit
from tg_control.adapters.base import Chat, ChatKind
from tg_control.auth import authenticate, is_active, revoke_client, rotate_credential
from tg_control.db import Account, AuditEvent, Client, Grant, now
from tg_control.policy import GrantInput, Permission, Scope, get_scope


def seed(session):
    account = Account(alias="Synthetic account", phone_encrypted="synthetic-ciphertext")
    client = Client(name="Synthetic client")
    session.add_all([account, client])
    session.flush()
    return account, client


def test_independent_scopes_and_exclusion_matrix(database):
    with database() as session:
        account, client = seed(session)
        read = Scope(
            enabled=True,
            allow_types=[ChatKind.PUBLIC_CHANNEL],
            exclude_chats=["blocked"],
            allow_chats=["private"],
        )
        write = Scope(enabled=True, allow_chats=["write-only"])
        grant = Grant(
            client_id=client.id,
            account_id=account.id,
            read_scope=read.model_dump(mode="json"),
            write_scope=write.model_dump(mode="json"),
            admin_scope=Scope().model_dump(mode="json"),
        )
        session.add(grant)
        session.commit()
        channel = Chat("visible", ChatKind.PUBLIC_CHANNEL, "synthetic")
        blocked = Chat("blocked", ChatKind.PUBLIC_CHANNEL, "synthetic")
        direct = Chat("private", ChatKind.PRIVATE_DIRECT, "synthetic")
        new_private = Chat("new-private", ChatKind.PRIVATE_DIRECT, "synthetic")
        readable = get_scope(session, client.id, account.id, Permission.READ)
        writable = get_scope(session, client.id, account.id, Permission.WRITE)
        assert readable.allows(channel) and readable.allows(direct)
        assert not readable.allows(blocked) and not readable.allows(new_private)
        assert not writable.allows(channel)
        assert get_scope(session, client.id, account.id, Permission.ADMIN) is None
        # Exclusions override explicitly listed chats as well as type grants.
        assert not Scope(
            enabled=True, allow_chats=["private"], exclude_types=[ChatKind.PRIVATE_DIRECT]
        ).allows(direct)
        other, _ = seed_with_names(session)
        assert get_scope(session, client.id, other.id, Permission.READ) is None
        account.enabled = False
        session.commit()
        assert get_scope(session, client.id, account.id, Permission.READ) is None


def seed_with_names(session):
    account = Account(alias="Synthetic other", phone_encrypted="synthetic-ciphertext")
    client = Client(name="Synthetic other client")
    session.add_all([account, client])
    session.flush()
    return account, client


def test_private_type_grants_are_rejected():
    with pytest.raises(ValueError):
        Scope(enabled=True, allow_types=["private_direct"])
    assert not GrantInput().read.enabled


def test_token_rotation_revocation_and_no_plaintext_storage(database):
    with database() as session:
        _, client = seed(session)
        token1 = rotate_credential(session, client.id)
        session.commit()
        principal = authenticate(session, token1)
        assert principal is not None and is_active(session, principal)
        from tg_control.db import Credential

        credential = session.get(Credential, principal.credential_id)
        assert token1 not in credential.token_digest
        token2 = rotate_credential(session, client.id)
        session.commit()
        assert authenticate(session, token1) is None
        assert not is_active(session, principal)
        assert authenticate(session, token2) is not None
        assert authenticate(session, token2 + "tamper") is None
        revoke_client(session, client.id)
        session.commit()
        assert authenticate(session, token2) is None


def test_audit_retention_and_metadata_only(database):
    with database() as session:
        audit.record(
            session,
            request_id="synthetic-request",
            actor_kind="client",
            actor_id=None,
            event="send",
            decision="allowed",
        )
        audit.record(
            session,
            request_id="synthetic-old",
            actor_kind="client",
            actor_id=None,
            event="send",
            decision="denied",
        )
        session.flush()
        old = session.scalar(select(AuditEvent).where(AuditEvent.request_id == "synthetic-old"))
        old.timestamp = now() - timedelta(days=91)
        session.commit()
        assert audit.prune(session, 90) == 1
        session.commit()
        events = session.scalars(select(AuditEvent)).all()
        assert [e.request_id for e in events] == ["synthetic-request"]
        assert not hasattr(events[0], "body")
        with pytest.raises(ValueError):
            audit.prune(session, 0)


def test_settings_require_valid_secrets():
    from tg_control.config import Settings

    with pytest.raises(ValueError):
        Settings("sqlite://", "short", Fernet.generate_key())
    with pytest.raises(ValueError):
        Settings("sqlite://", "x" * 32, b"bad")
    with pytest.raises(ValueError):
        Settings("sqlite://", "x" * 32, Fernet.generate_key(), allowed_hosts=("*",))


def test_secret_files_require_private_permissions_and_bounded_content(tmp_path):
    from tg_control.config import _read_secret

    path = tmp_path / "synthetic-secret"
    path.write_text("test-secret")
    path.chmod(0o600)
    assert _read_secret(str(path)) == "test-secret"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        _read_secret(str(path))
    path.chmod(0o600)
    path.write_text("x" * 8193)
    with pytest.raises(ValueError, match="too large"):
        _read_secret(str(path))
