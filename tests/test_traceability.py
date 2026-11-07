"""The traceability matrix must reference real files and real executed tests."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "TRACEABILITY.md"
TESTS = ROOT / "tests"


def matrix_text():
    assert MATRIX.exists(), "docs/TRACEABILITY.md is missing"
    return MATRIX.read_text()


def test_every_matrix_reference_exists():
    text = matrix_text()
    references = set(re.findall(r"`([^`\s]+)`", text))
    checked = 0
    for reference in sorted(references):
        if reference.startswith(("src/", "docs/", "alembic/", "tests/")) and "::" not in reference:
            assert (ROOT / reference).exists(), f"missing referenced path: {reference}"
            checked += 1
        elif reference.startswith("tests/test_") and "::" not in reference:
            assert (ROOT / reference).exists(), f"missing referenced file: {reference}"
            checked += 1
    assert checked > 20


def test_every_listed_test_function_exists():
    text = matrix_text()
    listed = re.findall(r"`(tests/test_[A-Za-z0-9_]+\.py)::([A-Za-z0-9_]+)`", text)
    assert listed, "no test functions listed"
    cache: dict[str, str] = {}
    for filename, name in listed:
        if filename not in cache:
            cache[filename] = (ROOT / filename).read_text()
        pattern = rf"^(?:async )?def {re.escape(name)}\("
        assert re.search(pattern, cache[filename], re.M), f"{filename}::{name} is gone"


def test_every_test_file_is_listed():
    text = matrix_text()
    listed_files = set(re.findall(r"`(tests/test_[A-Za-z0-9_]+\.py)`", text))
    actual = {path.relative_to(ROOT).as_posix() for path in TESTS.glob("test_*.py")}
    missing = sorted(actual - listed_files)
    assert not missing, f"test files absent from the matrix: {missing}"


def test_matrix_does_not_claim_live_evidence():
    text = matrix_text().lower()
    for phrase in (
        "production-ready",
        "production ready",
        "live integration verified",
        "real telegram data used",
    ):
        assert phrase not in text
