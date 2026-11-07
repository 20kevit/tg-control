"""Independent random bearer credentials; only SHA-256 digests are persisted."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import Client, Credential, new_id, now


@dataclass(frozen=True)
class Principal:
    client_id: str
    credential_id: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_owner(candidate: str, configured: str) -> bool:
    # Compare digests so differing string lengths aren't exposed to compare_digest.
    return hmac.compare_digest(_digest(candidate), _digest(configured))


def authenticate(session: Session, token: str) -> Principal | None:
    if len(token) > 256 or not token.startswith("tgc_"):
        return None
    pieces = token.split(".", 1)
    if len(pieces) != 2:
        return None
    credential_id = pieces[0][4:]
    if len(credential_id) != 36:
        return None
    credential = session.get(Credential, credential_id)
    if not credential or credential.revoked:
        return None
    client = session.get(Client, credential.client_id)
    if (
        not client
        or not client.enabled
        or not hmac.compare_digest(credential.token_digest, _digest(token))
    ):
        return None
    return Principal(client.id, credential.id)


def is_active(session: Session, principal: Principal) -> bool:
    # Refresh across transactions: revocation or account changes are not cached.
    session.expire_all()
    credential = session.get(Credential, principal.credential_id)
    client = session.get(Client, principal.client_id)
    return bool(
        credential
        and not credential.revoked
        and credential.client_id == principal.client_id
        and client
        and client.enabled
    )


def rotate_credential(session: Session, client_id: str) -> str:
    # PostgreSQL row locking serializes concurrent rotations for the same client.
    client = session.execute(
        select(Client).where(Client.id == client_id).with_for_update()
    ).scalar_one_or_none()
    if not client or not client.enabled:
        raise LookupError("client not found or disabled")
    session.execute(
        update(Credential)
        .where(Credential.client_id == client_id, Credential.revoked.is_(False))
        .values(revoked=True, revoked_at=now())
    )
    credential_id = new_id()
    token = f"tgc_{credential_id}.{secrets.token_urlsafe(32)}"
    session.add(Credential(id=credential_id, client_id=client_id, token_digest=_digest(token)))
    session.flush()
    return token


def revoke_client(session: Session, client_id: str) -> None:
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError("client not found")
    client.enabled = False
    session.execute(
        update(Credential)
        .where(Credential.client_id == client_id, Credential.revoked.is_(False))
        .values(revoked=True, revoked_at=now())
    )
