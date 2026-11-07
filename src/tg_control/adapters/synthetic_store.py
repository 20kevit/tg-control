"""Encrypted per-account persistence for synthetic data only.

This is not TDLib storage. No user-supplied filenames are used as paths.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SyntheticStore:
    def __init__(self, root: Path, master_key: bytes):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("synthetic state path must be a directory")
        if os.name == "posix" and self.root.stat().st_mode & 0o077:
            raise ValueError("synthetic state directory must not be group/world accessible")
        self._master = base64.urlsafe_b64decode(master_key)
        if len(self._master) != 32:
            raise ValueError("invalid encryption key")
        self._closed = False
        self._lock_fd: int | None = None
        if os.name == "posix":
            import fcntl

            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise RuntimeError("synthetic state directory is already in use") from None
            self._lock_fd = fd

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._closed = True

    def _directory(self, account_id: str) -> Path:
        if self._closed:
            raise RuntimeError("synthetic state store is closed")
        parsed = UUID(account_id)
        if str(parsed) != account_id:
            raise ValueError("invalid account ID")
        directory = self.root / account_id
        if directory.is_symlink():
            raise ValueError("account directory cannot be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix" and directory.stat().st_mode & 0o077:
            raise ValueError("account directory must not be group/world accessible")
        if directory.resolve().parent != self.root:
            raise ValueError("account directory is outside state root")
        return directory

    def _fernet(self, account_id: str) -> Fernet:
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=UUID(account_id).bytes,
            info=b"tg-control/synthetic-account/v1",
        ).derive(self._master)
        return Fernet(base64.urlsafe_b64encode(key))

    def load(self, account_id: str) -> dict | None:
        path = self._directory(account_id) / "state.enc"
        if path.is_symlink():
            raise ValueError("state file cannot be a symlink")
        if not path.exists():
            return None
        encrypted = path.read_bytes()
        value = json.loads(self._fernet(account_id).decrypt(encrypted))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("unsupported synthetic state")
        return value

    def save(self, account_id: str, value: dict) -> None:
        directory = self._directory(account_id)
        path = directory / "state.enc"
        if path.is_symlink():
            raise ValueError("state file cannot be a symlink")
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet(account_id).encrypt(payload)
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            # Persist the directory rename on systems that support it.
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
