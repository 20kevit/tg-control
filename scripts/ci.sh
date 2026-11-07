#!/usr/bin/env bash
# Local CI gate. Fails closed on any drift, lint error or failing test.
# Usage: scripts/ci.sh   (all evidence is synthetic; no live Telegram access)
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then PYTHON="python3"; fi

echo "== dependency consistency =="
"$PYTHON" -m pip check

echo "== static analysis =="
"$PYTHON" -m ruff check src tests alembic scripts

echo "== interface contract gate =="
"$PYTHON" scripts/check_interfaces.py

echo "== traceability matrix =="
"$PYTHON" - <<'PY'
import re
from pathlib import Path
text = Path("docs/TRACEABILITY.md").read_text()
actual = {p.as_posix() for p in Path("tests").glob("test_*.py")}
listed = set(re.findall(r"`(tests/test_[A-Za-z0-9_]+\.py)`", text))
missing = sorted(actual - listed)
if missing:
    raise SystemExit(f"test files missing from docs/TRACEABILITY.md: {missing}")
print(f"traceability matrix covers {len(actual)} test files")
PY

echo "== test suite =="
"$PYTHON" -m pytest -q

echo "== done: all gates passed =="
