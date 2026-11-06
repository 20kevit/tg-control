"""Fail-closed configuration. There is intentionally no live-adapter switch."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet


def _read_secret(path: str) -> str:
    with Path(path).open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise ValueError("secret file must be regular and private")
        raw = stream.read(8193).strip()
    if not raw or len(raw) > 8192:
        raise ValueError("secret file is empty or too large")
    return raw.decode("utf-8")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    owner_token: str = field(repr=False)
    encryption_key: bytes = field(repr=False)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ("http://localhost:8000", "http://127.0.0.1:8000")
    operation_timeout_seconds: float = 30.0
    max_request_bytes: int = 2 * 1024 * 1024
    synthetic_state_dir: Path | None = None

    def __post_init__(self) -> None:
        if len(self.owner_token) < 32:
            raise ValueError("owner token must contain at least 32 characters")
        Fernet(self.encryption_key)  # validate without displaying key material
        if not self.allowed_hosts or any("*" in host for host in self.allowed_hosts):
            raise ValueError("explicit allowed hosts are required")
        if any("*" in origin for origin in self.allowed_origins):
            raise ValueError("wildcard origins are not permitted")
        if not (0 < self.operation_timeout_seconds <= 300):
            raise ValueError("invalid operation timeout")
        if not (1024 <= self.max_request_bytes <= 20 * 1024 * 1024):
            raise ValueError("invalid maximum request size")

    @classmethod
    def from_env(cls) -> Settings:
        # Database URI may include a password; a mounted file is preferred.
        db_file = os.environ.get("TG_DATABASE_URL_FILE")
        database_url = _read_secret(db_file) if db_file else os.environ["TG_DATABASE_URL"]
        owner = _read_secret(os.environ["TG_OWNER_TOKEN_FILE"])
        key = _read_secret(os.environ["TG_ENCRYPTION_KEY_FILE"]).encode("ascii")
        hosts = tuple(
            x.strip()
            for x in os.getenv("TG_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
            if x.strip()
        )
        origins = tuple(
            x.strip()
            for x in os.getenv(
                "TG_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
            ).split(",")
            if x.strip()
        )
        state_dir = os.getenv("TG_SYNTHETIC_STATE_DIR")
        return cls(
            database_url,
            owner,
            key,
            hosts,
            origins,
            synthetic_state_dir=Path(state_dir) if state_dir else None,
        )
