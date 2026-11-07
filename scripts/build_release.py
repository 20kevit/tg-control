#!/usr/bin/env python
"""Build a local release candidate without publishing anything.

Produces, in the output directory:
    tg_control-<version>-py3-none-any.whl
    SHA256SUMS                              (artifact digests)
    release-provenance.json                 (version, revision, dirty flag, hashes)

`--publish` is deliberately unsupported: publishing to a registry, creating a
GitHub release or deploying requires separate owner approval. The script exits
non-zero if asked to publish so an automated caller cannot do it by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tg_control.db import EXPECTED_SCHEMA_REVISION  # noqa: E402
from tg_control.version import __version__  # noqa: E402


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def source_revision() -> str:
    return git("rev-parse", "HEAD") or os.environ.get("TG_BUILD_REVISION", "unknown")


def dirty() -> bool:
    return bool(git("status", "--porcelain"))


def build(output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    wheel_dir = Path(tempfile.mkdtemp(prefix="tg-control-wheel-"))
    # Build isolation downloads the build backend (setuptools/wheel) into a
    # temporary environment. Runtime dependencies are still fixed by
    # requirements.lock; the build backend is not part of the shipped artifact.
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheel_dir)],
        cwd=ROOT,
        check=True,
    )
    artifacts = sorted(wheel_dir.glob("*.whl"))
    if not artifacts:
        raise SystemExit("no wheel produced")
    for artifact in artifacts:
        shutil.copy2(artifact, output / artifact.name)
    shutil.rmtree(wheel_dir, ignore_errors=True)
    return sorted(output.glob("*.whl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--dry-run", action="store_true", help="print provenance only")
    parser.add_argument(
        "--publish", action="store_true", help="unsupported; requires owner approval"
    )
    args = parser.parse_args(argv)
    if args.publish:
        print(
            "refusing to publish: building is allowed locally, publishing a wheel or image "
            "requires explicit owner approval",
            file=sys.stderr,
        )
        return 2
    provenance = {
        "version": __version__,
        "schema_revision": EXPECTED_SCHEMA_REVISION,
        "source_revision": source_revision(),
        "dirty": dirty(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "publish": False,
        "artifacts": {},
    }
    if args.dry_run:
        print(json.dumps(provenance, indent=2, sort_keys=True))
        return 0
    artifacts = build(args.output_dir)
    sums = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        provenance["artifacts"][artifact.name] = digest
        sums.append(f"{digest}  {artifact.name}")
    (args.output_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (args.output_dir / "release-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True)
    )
    print(json.dumps(provenance, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
