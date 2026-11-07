"""Streaming media staging, validation and lifecycle (synthetic-only).

The pipeline never talks to Telegram. Callers supply bytes that were already
authorized and retrieved through Gateway Core. It provides chunked staging,
untrusted metadata validation, signature checks, SHA-256 integrity, quotas,
private temporary storage, cancellation-safe cleanup and age-based removal.

Design notes (see ADR-008):
- Caller-provided filenames and MIME types are untrusted claims. They are
  validated and used only as metadata; storage names are server-generated.
- Paths are server-owned UUIDs under a private root. No caller can supply a path,
  a directory or a symlink target, and files are opened with ``O_NOFOLLOW``.
- Transfer is chunk-based so a future native adapter can stream. The synthetic
  adapter materializes one chunk, which is explicitly labelled as a limitation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from uuid import UUID

CHUNK_SIZE = 64 * 1024
MAX_FILENAME_LENGTH = 255
MAX_MEDIA_BYTES = 1024 * 1024  # synthetic SendMedia contract limit
DEFAULT_ACCOUNT_QUOTA = 16 * 1024 * 1024
DEFAULT_TOTAL_QUOTA = 64 * 1024 * 1024
DEFAULT_CONCURRENCY = 4
DEFAULT_TTL_SECONDS = 3600

# content type -> accepted magic prefixes (empty means "no signature required")
SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "text/plain": (),
    "text/csv": (),
    "application/json": (),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    "application/zip": (b"PK\x03\x04",),
    "audio/ogg": (b"OggS",),
}
MIME_PATTERN = re.compile(r"^[a-z0-9]+/[a-z0-9.+-]{1,64}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class MediaError(Exception):
    """Normalized media failure. Never include upstream text or local paths."""

    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class MediaLimits:
    max_file_bytes: int = MAX_MEDIA_BYTES
    account_bytes: int = DEFAULT_ACCOUNT_QUOTA
    total_bytes: int = DEFAULT_TOTAL_QUOTA
    concurrency: int = DEFAULT_CONCURRENCY
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def __post_init__(self) -> None:
        if not (1024 <= self.max_file_bytes <= 64 * 1024 * 1024):
            raise ValueError("invalid per-file media limit")
        if self.account_bytes < self.max_file_bytes:
            raise ValueError("account quota must cover one file")
        if self.total_bytes < self.account_bytes:
            raise ValueError("total quota must cover one account quota")
        if not (1 <= self.concurrency <= 32):
            raise ValueError("invalid media concurrency")
        if not (60 <= self.ttl_seconds <= 7 * 86400):
            raise ValueError("invalid media staging TTL")


@dataclass(frozen=True)
class StagedMedia:
    id: str
    account_id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    created_at: float
    _path: Path

    def read_all(self) -> bytes:
        """Read a bounded staged file. Size is validated at staging time."""
        if self.size > MAX_MEDIA_BYTES:
            raise MediaError("unsupported_media", 413)
        return self._path.read_bytes()

    def as_base64(self) -> str:
        return base64.b64encode(self.read_all()).decode("ascii")

    async def chunks(self, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        if not (1 <= chunk_size <= 1024 * 1024):
            raise ValueError("invalid chunk size")
        with self._path.open("rb") as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
                await asyncio.sleep(0)


def _validate_filename(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_FILENAME_LENGTH:
        raise MediaError("invalid_filename", 422)
    if value in {".", ".."} or CONTROL_CHARS.search(value):
        raise MediaError("invalid_filename", 422)
    if any(component in value for component in ("/", "\\", "\x00")):
        raise MediaError("invalid_filename", 422)
    if value.startswith(".") or value.endswith((".", " ")):
        raise MediaError("invalid_filename", 422)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # pragma: no cover - str is always encodable
        raise MediaError("invalid_filename", 422) from None
    return value


def _validate_content_type(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise MediaError("unsupported_media", 415)
    lowered = value.strip().lower()
    if not MIME_PATTERN.match(lowered) or lowered not in SIGNATURES:
        raise MediaError("unsupported_media", 415)
    return lowered


def _check_signature(content_type: str, first: bytes) -> None:
    signatures = SIGNATURES.get(content_type, ())
    if not signatures:
        if b"\x00" in first:
            raise MediaError("invalid_media", 422)
        return
    if not first.startswith(signatures) and not any(
        first.startswith(signature) for signature in signatures
    ):
        raise MediaError("invalid_media", 422)


def _account_dir(root: Path, account_id: str) -> Path:
    try:
        parsed = UUID(account_id)
    except (ValueError, TypeError):
        raise MediaError("unknown_account", 404) from None
    if str(parsed) != account_id:
        raise MediaError("unknown_account", 404)
    directory = root / account_id
    if directory.exists() and directory.is_symlink():
        raise MediaError("invalid_media", 500)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


class MediaPipeline:
    def __init__(
        self,
        root: Path,
        *,
        limits: MediaLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time as _time

        self.limits = limits or MediaLimits()
        self._clock = clock or _time.time
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("media staging root must be a private directory")
        if os.name == "posix" and self.root.stat().st_mode & 0o077:
            raise ValueError("media staging root must not be group/world accessible")
        self._semaphore = asyncio.Semaphore(self.limits.concurrency)
        self._active = 0

    def _usage(self, directory: Path) -> int:
        total = 0
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            total += entry.stat().st_size
        return total

    def account_usage(self, account_id: str) -> int:
        return self._usage(_account_dir(self.root, account_id))

    def total_usage(self) -> int:
        total = 0
        for entry in self.root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                total += self._usage(entry)
        return total

    async def stage(
        self,
        account_id: str,
        payload: Mapping[str, Any],
        *,
        guard: Callable[[], Awaitable[None]] | None = None,
        declared_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedMedia:
        """Stage a media payload from an already-authorized Core read."""
        if not isinstance(payload, Mapping):
            raise MediaError("invalid_media", 422)
        try:
            data = base64.b64decode(payload["data_base64"], validate=True)
        except (KeyError, ValueError, binascii.Error, TypeError):
            raise MediaError("invalid_media", 422) from None
        chunks = _single_chunk(data)

        async def iterator() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        return await self._stage_stream(
            account_id,
            payload.get("filename"),
            payload.get("content_type"),
            iterator(),
            guard=guard,
            declared_size=int(payload["size"])
            if isinstance(payload.get("size"), int)
            else declared_size,
            expected_sha256=payload.get("sha256")
            if isinstance(payload.get("sha256"), str)
            else expected_sha256,
        )

    async def stage_stream(
        self,
        account_id: str,
        filename: object,
        content_type: object,
        source: AsyncIterator[bytes],
        *,
        guard: Callable[[], Awaitable[None]] | None = None,
        declared_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StagedMedia:
        return await self._stage_stream(
            account_id,
            filename,
            content_type,
            source,
            guard=guard,
            declared_size=declared_size,
            expected_sha256=expected_sha256,
        )

    async def _stage_stream(
        self,
        account_id: str,
        filename: object,
        content_type: object,
        source: AsyncIterator[bytes],
        *,
        guard: Callable[[], Awaitable[None]] | None,
        declared_size: int | None,
        expected_sha256: str | None,
    ) -> StagedMedia:
        safe_filename = _validate_filename(filename)
        safe_type = _validate_content_type(content_type)
        if declared_size is not None and (
            type(declared_size) is not int or declared_size < 0
        ):
            raise MediaError("invalid_media", 422)
        if declared_size is not None and declared_size > self.limits.max_file_bytes:
            raise MediaError("media_too_large", 413)
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256.lower())
        ):
            raise MediaError("invalid_media", 422)
        directory = _account_dir(self.root, account_id)
        if self._usage(directory) + (declared_size or 0) > self.limits.account_bytes:
            raise MediaError("quota_exceeded", 413)
        if self.total_usage() + (declared_size or 0) > self.limits.total_bytes:
            raise MediaError("quota_exceeded", 413)
        if guard is not None:
            await guard()
        async with self._semaphore:
            self._active += 1
            media_id = uuid.uuid4().hex
            part = directory / f"{media_id}.part"
            final = directory / f"{media_id}.bin"
            created = self._clock()
            digest = hashlib.sha256()
            size = 0
            try:
                fd = os.open(
                    part,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    first = True
                    async for chunk in source:
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise MediaError("invalid_media", 422)
                        chunk = bytes(chunk)
                        if first:
                            _check_signature(safe_type, chunk)
                            first = False
                        size += len(chunk)
                        if size > self.limits.max_file_bytes:
                            raise MediaError("media_too_large", 413)
                        digest.update(chunk)
                        os.write(fd, chunk)
                        if guard is not None:
                            await guard()
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if size == 0:
                    raise MediaError("invalid_media", 422)
                if declared_size is not None and size != declared_size:
                    raise MediaError("size_mismatch", 422)
                computed = digest.hexdigest()
                if expected_sha256 is not None and expected_sha256.lower() != computed:
                    raise MediaError("checksum_mismatch", 422)
                # Verify what is on disk, not only what passed through the buffer.
                verify = hashlib.sha256()
                with part.open("rb") as stream:
                    while True:
                        block = stream.read(CHUNK_SIZE)
                        if not block:
                            break
                        verify.update(block)
                if verify.hexdigest() != computed:
                    raise MediaError("checksum_mismatch", 500)
                os.replace(part, final)
                if guard is not None:
                    await guard()
            except asyncio.CancelledError:
                _unlink(part)
                _unlink(final)
                raise
            except BaseException:
                _unlink(part)
                _unlink(final)
                raise
            finally:
                self._active -= 1
            return StagedMedia(
                id=media_id,
                account_id=account_id,
                filename=safe_filename,
                content_type=safe_type,
                size=size,
                sha256=computed,
                created_at=created,
                _path=final,
            )

    def release(self, media: StagedMedia) -> None:
        if not isinstance(media, StagedMedia):
            raise TypeError("invalid staged media")
        try:
            expected_parent = _account_dir(self.root, media.account_id)
        except MediaError:  # pragma: no cover - account id came from this pipeline
            return
        if media._path.parent != expected_parent:
            return
        _unlink(media._path)

    def cleanup(self, *, ttl_seconds: int | None = None) -> int:
        """Remove expired staged files and abandoned partial writes."""
        ttl = self.limits.ttl_seconds if ttl_seconds is None else ttl_seconds
        if type(ttl) is not int or ttl < 60:
            raise ValueError("invalid cleanup TTL")
        cutoff = self._clock() - ttl
        removed = 0
        for account_dir in self.root.iterdir():
            if account_dir.is_symlink() or not account_dir.is_dir():
                continue
            for entry in account_dir.iterdir():
                if entry.is_symlink() or not entry.is_file():
                    continue
                if entry.suffix not in {".bin", ".part"}:
                    continue
                try:
                    if entry.stat().st_mtime <= cutoff:
                        _unlink(entry)
                        removed += 1
                except OSError:  # pragma: no cover - concurrent removal
                    continue
        return removed


def _single_chunk(data: bytes) -> list[bytes]:
    # Synthetic adapter payloads are already in memory; chunking keeps the write
    # path identical to a streaming adapter and bounds each write.
    return [data[index : index + CHUNK_SIZE] for index in range(0, len(data), CHUNK_SIZE)]


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - permission or race
        pass
