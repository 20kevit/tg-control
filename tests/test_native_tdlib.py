"""Opt-in feasibility test. No Telegram login, API credentials, or network access."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_native_tdjson_offline_two_client_correlation():
    library = os.environ.get("TG_TDLIB_LIBRARY")
    if not library:
        pytest.skip("native TDLib not supplied; synthetic suite does not require it")
    probe = Path(__file__).with_name("tdlib_offline_probe.py")
    # Pass no gateway secrets or other application configuration to the subprocess.
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    result = subprocess.run(
        [sys.executable, str(probe), library],
        env=environment,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "network syscalls blocked" in result.stdout
