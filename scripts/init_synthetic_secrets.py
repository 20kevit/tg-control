"""Create new local synthetic-development secrets without printing or overwriting them."""

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("secrets"))
    args = parser.parse_args()
    directory = args.directory
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        parser.error("secret directory must be a real directory")
    if os.name == "posix" and stat.S_IMODE(directory.stat().st_mode) & 0o077:
        parser.error("secret directory must not be group/world accessible")
    names = [
        "postgres_password",
        "database_url",
        "backup_database_url",
        "owner_token",
        "encryption_key",
        "backup_key",
    ]
    if any((directory / name).exists() or (directory / name).is_symlink() for name in names):
        parser.error("a target already exists; no existing secret will be overwritten")
    password = secrets.token_urlsafe(32)
    values = {
        "postgres_password": password.encode(),
        "database_url": f"postgresql+psycopg://tgcontrol:{password}@db:5432/tgcontrol".encode(),
        "backup_database_url": (
            f"postgresql+psycopg://tgcontrol:{password}@127.0.0.1:55432/tgcontrol".encode()
        ),
        "owner_token": secrets.token_urlsafe(48).encode(),
        "encryption_key": Fernet.generate_key(),
        "backup_key": Fernet.generate_key(),
    }
    try:
        for name, value in values.items():
            fd = os.open(
                directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as target:
                target.write(value + b"\n")
                target.flush()
                os.fsync(target.fileno())
    except OSError:
        print(
            "Could not create every file; inspect the private directory before retrying.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Local synthetic-development secrets created; protect them and back up keys separately.")


if __name__ == "__main__":
    main()
