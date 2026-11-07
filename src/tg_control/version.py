"""Build and version metadata (single source of truth).

The version is intentionally not read from Git at runtime: an installed release
must be able to report what it is without a checkout, and a repository checkout
must not silently report a different version than the wheel it produced.
`TG_BUILD_REVISION` / `TG_BUILD_TIME` are injected by `scripts/build_release.py`
and are informational only; nothing security-relevant depends on them.
"""

from __future__ import annotations

import os

__version__ = "0.1.0"
CONTRACT_SCHEMA_VERSION = 1


def build_info(schema_revision: str) -> dict[str, str]:
    """Return non-sensitive build metadata for diagnostics."""
    return {
        "version": __version__,
        "schema_revision": schema_revision,
        "source_revision": os.environ.get("TG_BUILD_REVISION", "unknown")[:64],
        "build_time": os.environ.get("TG_BUILD_TIME", "unknown")[:32],
    }
