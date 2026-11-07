"""Media pipeline acceptance tests (synthetic bytes, private staging)."""

import asyncio
import base64
import hashlib
import os
import time
import uuid

import pytest

from tg_control.media import MediaError, MediaLimits, MediaPipeline

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-body"
TEXT = b"synthetic text body\n"


def pipeline(tmp_path, **limits):
    return MediaPipeline(tmp_path / "media", limits=MediaLimits(**limits))


def payload(data=TEXT, filename="note.txt", content_type="text/plain", **overrides):
    body = {
        "filename": filename,
        "content_type": content_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "size": len(data),
    }
    body.update(overrides)
    return body


async def test_stage_verifies_checksum_and_releases(tmp_path):
    service = pipeline(tmp_path)
    staged = await service.stage(str(uuid.uuid4()), payload())
    assert staged.size == len(TEXT)
    assert staged.sha256 == hashlib.sha256(TEXT).hexdigest()
    assert staged.filename == "note.txt" and staged.content_type == "text/plain"
    assert staged._path.exists()
    assert not staged._path.is_symlink()
    if os.name == "posix":
        assert staged._path.stat().st_mode & 0o077 == 0
    collected = b"".join([chunk async for chunk in staged.chunks(4)])
    assert collected == TEXT
    assert staged.as_base64() == payload()["data_base64"]
    service.release(staged)
    assert not staged._path.exists()
    service.release(staged)


async def test_untrusted_filename_and_content_type_are_validated(tmp_path):
    service = pipeline(tmp_path)
    account = str(uuid.uuid4())
    for filename in ["../escape.txt", "dir/file.txt", "bad\x00name", ".hidden", "trail."]:
        with pytest.raises(MediaError) as exc:
            await service.stage(account, payload(filename=filename))
        assert exc.value.code == "invalid_filename"
    for content_type in ["application/x-executable", "image/svg+xml", "text/plain; charset=utf-8"]:
        with pytest.raises(MediaError) as exc:
            await service.stage(account, payload(content_type=content_type))
        assert exc.value.code == "unsupported_media"
    with pytest.raises(MediaError) as bad_account:
        await service.stage("not-a-uuid", payload())
    assert bad_account.value.code == "unknown_account"
    with pytest.raises(MediaError) as not_base64:
        await service.stage(account, payload(data_base64="not base64", size=1))
    assert not_base64.value.code == "invalid_media"


async def test_signature_size_and_checksum_mismatches_are_rejected(tmp_path):
    service = pipeline(tmp_path)
    account = str(uuid.uuid4())
    with pytest.raises(MediaError) as signature:
        await service.stage(
            account, payload(data=TEXT, filename="image.png", content_type="image/png")
        )
    assert signature.value.code == "invalid_media"
    with pytest.raises(MediaError) as declared:
        await service.stage(account, payload(size=len(TEXT) + 10))
    assert declared.value.code == "size_mismatch"
    with pytest.raises(MediaError) as checksum:
        await service.stage(account, payload(sha256="0" * 64))
    assert checksum.value.code == "checksum_mismatch"
    with pytest.raises(MediaError) as bad_checksum:
        await service.stage(account, payload(sha256="short"))
    assert bad_checksum.value.code == "invalid_media"
    # Nothing partial remains after any rejection.
    assert service.account_usage(account) == 0


async def test_limits_and_quotas_are_enforced(tmp_path):
    service = pipeline(tmp_path, max_file_bytes=1024, account_bytes=2048, total_bytes=4096)
    account = str(uuid.uuid4())
    with pytest.raises(MediaError) as too_large:
        await service.stage(account, payload(data=b"x" * 1025, size=1025))
    assert too_large.value.code == "media_too_large"
    await service.stage(account, payload(data=b"a" * 1024, size=1024))
    await service.stage(account, payload(data=b"b" * 1024, size=1024))
    with pytest.raises(MediaError) as quota:
        await service.stage(account, payload(data=b"c" * 1024, size=1024))
    assert quota.value.code == "quota_exceeded"


async def test_streaming_cancellation_leaves_no_partial_files(tmp_path):
    service = pipeline(tmp_path)
    account = str(uuid.uuid4())

    async def source():
        yield b"synthetic "
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.stage_stream(
            account, "stream.txt", "text/plain", source(), declared_size=10
        )
    assert list((tmp_path / "media" / account).iterdir()) == []


async def test_guard_failure_aborts_staging_and_cleanup_expires_files(tmp_path):
    service = pipeline(tmp_path)
    account = str(uuid.uuid4())

    async def denied():
        raise MediaError("denied", 403)

    with pytest.raises(MediaError):
        await service.stage(account, payload(), guard=denied)
    assert service.account_usage(account) == 0

    class Clock:
        value = time.time()

        def __call__(self):
            return self.value

    clock = Clock()
    service = MediaPipeline(tmp_path / "media2", clock=clock)
    staged = await service.stage(account, payload())
    clock.value += 120
    assert service.cleanup(ttl_seconds=60) == 1
    assert not staged._path.exists()
    second = await service.stage(account, payload())
    clock.value += 3600
    assert service.cleanup(ttl_seconds=3600) == 1
    assert not second._path.exists()
    with pytest.raises(ValueError):
        service.cleanup(ttl_seconds=1)


def test_root_must_be_private_and_not_a_symlink(tmp_path):
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        MediaPipeline(link)
    with pytest.raises(ValueError):
        MediaLimits(max_file_bytes=100)
    with pytest.raises(ValueError):
        MediaLimits(max_file_bytes=2048, account_bytes=1024)
    account_dir = tmp_path / "media-symlink"
    account_dir.mkdir(mode=0o700)
    service = MediaPipeline(account_dir)
    directory = account_dir / "00000000-0000-0000-0000-000000000000"
    directory.symlink_to(tmp_path)
    with pytest.raises(MediaError):
        service.account_usage("00000000-0000-0000-0000-000000000000")


async def test_multi_chunk_streaming_keeps_memory_bounded_and_verifies_digest(tmp_path):
    """A large synthetic transfer arrives in bounded chunks, not one buffer.

    The pipeline must accept an async iterator of arbitrary chunking, enforce the
    declared size, verify the digest and expose the staged bytes back as chunks
    without holding the whole file in one contiguous buffer.
    """
    import hashlib

    service = pipeline(tmp_path)
    account = str(uuid.uuid4())
    payload = b"synthetic-stream-" * 4096  # ~64 KiB
    digest = hashlib.sha256(payload).hexdigest()
    observed_chunks = []

    async def source():
        for offset in range(0, len(payload), 4096):
            chunk = payload[offset : offset + 4096]
            observed_chunks.append(len(chunk))
            yield chunk

    staged = await service.stage_stream(
        account,
        "stream.txt",
        "text/plain",
        source(),
        declared_size=len(payload),
        expected_sha256=digest,
    )
    assert len(observed_chunks) == -(-len(payload) // 4096) > 1
    assert staged.size == len(payload)
    assert staged.sha256 == digest
    returned = b"".join([chunk async for chunk in staged.chunks(chunk_size=8192)])
    assert returned == payload

    # A declared size that disagrees with the stream is rejected, not truncated.
    async def short():
        yield b"synthetic"

    with pytest.raises(MediaError) as mismatch:
        await service.stage_stream(
            account, "short.txt", "text/plain", short(), declared_size=64
        )
    assert mismatch.value.code == "size_mismatch"
