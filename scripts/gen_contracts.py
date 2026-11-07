"""Regenerate the machine-readable interface compatibility matrix.

Usage: python scripts/gen_contracts.py
Verified by tests/test_contracts.py (registry, JSON artifact and live app agree).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_control.contracts import compatibility_report  # noqa: E402

TARGET = ROOT / "docs" / "contracts" / "interfaces.json"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    payload = compatibility_report()
    TARGET.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(payload['operations'])} operations)")


if __name__ == "__main__":
    main()
