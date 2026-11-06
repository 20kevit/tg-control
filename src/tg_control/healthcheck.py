"""Authenticate the container's loopback-only health probe without logging secrets."""

from __future__ import annotations

import os
import sys
from urllib.request import ProxyHandler, Request, build_opener

from .config import _read_secret


def main() -> int:
    try:
        port = int(os.environ.get("TG_HEALTH_PORT", "8000"))
        if not 0 < port < 65536:
            raise ValueError("invalid port")
        token = _read_secret(os.environ["TG_OWNER_TOKEN_FILE"])
        request = Request(
            f"http://127.0.0.1:{port}/health/ready", headers={"Authorization": f"Bearer {token}"}
        )
        # Never use proxy environment variables for a local privileged probe.
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("not ready")
        return 0
    except Exception:
        # Do not print exception text: local paths, headers or URLs can contain secrets.
        print("health check failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
