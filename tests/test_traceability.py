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


EVIDENCE_LABELS = {
    "Implemented",
    "Locally tested",
    "Container-tested",
    "Hosted-CI-tested",
    "Not tested",
    "Blocked",
    "Requires owner action",
}


def matrix_rows():
    rows = []
    for line in matrix_text().splitlines():
        if not line.startswith("| ") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def test_every_area_row_has_an_evidence_label_and_a_contract():
    rows = [row for row in matrix_rows() if len(row) == 6 and row[0] != "Area"]
    assert len(rows) >= 20, "area table is missing or truncated"
    for row in rows:
        assert row[5] in EVIDENCE_LABELS, f"unknown evidence label {row[5]!r} in {row[0]!r}"
        assert row[1], f"area {row[0]!r} has no contract reference"
        assert int(row[4]) > 0, f"area {row[0]!r} claims no executed tests"


def test_hosted_pipeline_evidence_has_revision_and_run():
    text = matrix_text()
    rows = [row for row in matrix_rows() if len(row) == 6 and row[0] != "Area"]
    hosted = [row for row in rows if row[5] == "Hosted-CI-tested"]
    assert hosted and all(row[0] == "Hosted CI pipeline" for row in hosted)
    assert "42401029613" in text
    assert "59951afad2c05a7aa114d691ee7e73a026272026" in text


def test_markdown_local_link_targets_exist():
    from urllib.parse import unquote, urlsplit

    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for document in documents:
        for target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            url = urlsplit(target.split()[0].strip("<>"))
            if not url.scheme and not url.netloc and url.path:
                assert (document.parent / unquote(url.path)).exists(), (
                    f"{document.relative_to(ROOT)}: missing link target {target}"
                )
