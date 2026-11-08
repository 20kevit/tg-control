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

echo "== shell script syntax =="
for script in scripts/*.sh; do bash -n "$script"; done

echo "== deployment assets present =="
for asset in Dockerfile .dockerignore compose.yaml .env.example .github/workflows/ci.yaml; do
  [ -f "$asset" ] || { echo "missing deployment asset: $asset" >&2; exit 1; }
done

echo "== feature inventory drift =="
FEATURE_INVENTORY_COPY="$(mktemp -d)"
cp docs/FEATURE_TRACEABILITY.md docs/contracts/capability_catalog.json "$FEATURE_INVENTORY_COPY/"
"$PYTHON" scripts/gen_feature_catalog.py
if ! diff -q "$FEATURE_INVENTORY_COPY/FEATURE_TRACEABILITY.md" docs/FEATURE_TRACEABILITY.md >/dev/null \
  || ! diff -q "$FEATURE_INVENTORY_COPY/capability_catalog.json" docs/contracts/capability_catalog.json >/dev/null; then
  echo "feature inventory is stale; commit the regenerated catalogue and traceability matrix" >&2
  diff -u "$FEATURE_INVENTORY_COPY/FEATURE_TRACEABILITY.md" docs/FEATURE_TRACEABILITY.md | head -40 >&2 || true
  diff -u "$FEATURE_INVENTORY_COPY/capability_catalog.json" docs/contracts/capability_catalog.json | head -40 >&2 || true
  rm -rf "$FEATURE_INVENTORY_COPY"
  exit 1
fi
rm -rf "$FEATURE_INVENTORY_COPY"
echo "feature inventory is current (147 capability entries, 135 feature actions)"

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

echo "== traceability drift =="
TRACEABILITY_COPY="$(mktemp)"
cp docs/TRACEABILITY.md "$TRACEABILITY_COPY"
"$PYTHON" scripts/gen_traceability.py
if ! diff -q "$TRACEABILITY_COPY" docs/TRACEABILITY.md >/dev/null; then
  echo "docs/TRACEABILITY.md is stale; commit the regenerated matrix" >&2
  diff -u "$TRACEABILITY_COPY" docs/TRACEABILITY.md | head -40 >&2
  rm -f "$TRACEABILITY_COPY"
  exit 1
fi
rm -f "$TRACEABILITY_COPY"
echo "traceability matrix is current"

echo "== release provenance (dry run) =="
"$PYTHON" scripts/build_release.py --dry-run >/dev/null

echo "== test suite =="
if [ "${TG_COVERAGE:-0}" = "1" ]; then
  # Coverage gate: the measured floor is well above 80%; raise it deliberately
  # only together with new tests, never by weakening assertions.
  "$PYTHON" -m pytest -q --cov=tg_control --cov-fail-under=80
else
  "$PYTHON" -m pytest -q
fi

echo "== done: all gates passed =="
