"""Credential lifecycle primitive; memory-only, not a production identity store."""
import hashlib
import hmac
import secrets
from dataclasses import dataclass


@dataclass
class Credential:
    client: str
    salt: bytes
    digest: bytes
    revoked: bool = False


class Credentials:
    def __init__(self):
        self.records = {}

    @staticmethod
    def digest(token, salt):
        return hashlib.scrypt(token.encode(), salt=salt, n=16384, r=8, p=1)

    def issue(self, client):
        identifier = secrets.token_hex(8)
        token = identifier + '.' + secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        self.records[identifier] = Credential(client, salt, self.digest(token, salt))
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        record = self.records.get(token.split('.')[0])
        if record and not record.revoked and hmac.compare_digest(record.digest, self.digest(token, record.salt)):
            return record.client
        return None

    def revoke(self, token):
        record = self.records.get(token.split('.')[0])
        if record:
            record.revoked = True

    def rotate(self, token):
        client = self.authenticate(token)
        if client is None:
            raise ValueError('invalid_credential')
        replacement = self.issue(client)
        self.revoke(token)
        return replacement
