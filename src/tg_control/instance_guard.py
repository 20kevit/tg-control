"""Cooperative single-gateway guard for synthetic metadata databases.

This is not distributed fencing. A lost guard cannot undo in-flight effects;
operators must still run exactly one gateway and stop writers for maintenance.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger("tg_control.instance_guard")

# PostgreSQL advisory-lock namespace dedicated to this gateway, not a secret.
_LOCK_CLASS = 0x54474354
_LOCK_OBJECT = 0x53494E47


class _GuardError(RuntimeError):
    """A deliberately sanitized startup failure."""


class InstanceGuard:
    """Hold one dedicated PostgreSQL session lock, or a local SQLite lock file."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._connection: Connection | None = None
        self._backend_pid: int | None = None
        self._fd: int | None = None
        self._path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._kind: str | None = None
        self._lost = False

    def acquire(self) -> None:
        if self._kind is not None or self._lost:
            raise RuntimeError("gateway instance guard cannot be reused")
        if self.engine.dialect.name == "postgresql":
            self._acquire_postgres()
        elif self.engine.dialect.name == "sqlite":
            self._acquire_sqlite()
        else:
            raise RuntimeError("unsupported database for gateway instance guard")

    def _acquire_postgres(self) -> None:
        connection = None
        try:
            # Keep a dedicated session open. Transaction-pooling proxies are
            # unsupported: their backend/session identity is not stable.
            connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:class_id, :object_id)"),
                {"class_id": _LOCK_CLASS, "object_id": _LOCK_OBJECT},
            ).scalar_one()
            if acquired is not True:
                connection.close()
                raise _GuardError("another gateway is already using this metadata database")
            self._connection = connection
            self._backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self._kind = "postgresql"
            if not self.check():
                raise _GuardError("gateway instance guard could not be verified")
        except _GuardError:
            if self._connection is not None:
                self.close()
            raise
        except Exception:
            if connection is not None:
                self._discard_connection(connection)
            self._connection = None
            self._lost = True
            raise RuntimeError("gateway instance guard could not be acquired") from None

    def _acquire_sqlite(self) -> None:
        database = self.engine.url.database
        if database in {None, "", ":memory:"} and not self.engine.url.query:
            # Each application owns its own in-memory database/engine.
            self._kind = "memory"
            return
        if (
            os.name != "posix"
            or not database
            or database.startswith("file:")
            or self.engine.url.query
        ):
            raise RuntimeError("unsupported SQLite database for gateway instance guard")
        import fcntl

        fd = None
        try:
            db_path = Path(database).resolve()
            path = db_path.with_name(db_path.name + ".tg-control.lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError("invalid lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd, self._path = fd, path
            self._identity = (info.st_dev, info.st_ino)
            self._kind = "sqlite"
            if not self.check():
                raise RuntimeError("gateway instance guard could not be verified")
        except BlockingIOError:
            if fd is not None:
                os.close(fd)
            raise RuntimeError("another gateway is already using this metadata database") from None
        except Exception:
            if fd is not None:
                os.close(fd)
            self._fd = None
            self._lost = True
            raise RuntimeError("gateway instance guard could not be acquired") from None

    def check(self) -> bool:
        """Fail permanently closed after loss; do not silently reacquire a new lock."""
        if self._lost or self._kind is None:
            return False
        try:
            if self._kind == "memory":
                return True
            if self._kind == "sqlite":
                info = os.stat(self._path, follow_symlinks=False)
                open_info = os.fstat(self._fd)
                held = (
                    stat.S_ISREG(info.st_mode)
                    and info.st_mode & 0o077 == 0
                    and info.st_nlink == 1
                    and (info.st_dev, info.st_ino) == self._identity
                    and (open_info.st_dev, open_info.st_ino) == self._identity
                )
            else:
                row = self._connection.execute(
                    text(
                        "SELECT pg_backend_pid(), EXISTS ("
                        "SELECT 1 FROM pg_catalog.pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                        "AND database = (SELECT oid FROM pg_catalog.pg_database "
                        "WHERE datname = current_database()) "
                        "AND classid = CAST(:class_id AS oid) AND objid = CAST(:object_id AS oid) "
                        "AND objsubid = 2 AND mode = 'ExclusiveLock' AND granted)"
                    ),
                    {"class_id": _LOCK_CLASS, "object_id": _LOCK_OBJECT},
                ).one()
                held = row[0] == self._backend_pid and row[1] is True
            if held:
                return True
        except Exception:
            pass  # Database/path errors are deliberately not logged or returned.
        self._lost = True
        logger.error("gateway instance guard lost; restart required")
        return False

    @staticmethod
    def _discard_connection(connection: Connection) -> None:
        # Do not return a session-level lock to the connection pool, and never
        # expose driver exception text (which may contain connection details).
        try:
            connection.invalidate()
        except Exception:
            logger.error("gateway instance guard connection cleanup failed")
        try:
            connection.close()
        except Exception:
            logger.error("gateway instance guard connection cleanup failed")

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                logger.error("gateway instance guard file cleanup failed")
            self._fd = None
        if self._connection is not None:
            self._discard_connection(self._connection)
            self._connection = None
        self._lost = True


class InstanceGuardBoundary:
    """Reject new HTTP work after guard loss; previously started work may finish."""

    def __init__(self, app, *, guard: InstanceGuard):
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not self.guard.check():
            body = b'{"error":"instance_guard_lost","outcome_unknown":false}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
