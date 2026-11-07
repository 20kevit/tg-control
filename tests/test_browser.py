"""Real Chromium end-to-end test; Node dependencies are installed via npm ci."""

import asyncio
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn

from tg_control.api import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_owner_panel_end_to_end_in_chromium(settings, tmp_path):
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = settings.database_url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    settings = replace(settings, allowed_hosts=("127.0.0.1",), allowed_origins=(origin,))
    ready = asyncio.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = TestServer(
        uvicorn.Config(
            create_app(settings),
            log_level="critical",
            access_log=False,
            lifespan="on",
            log_config=None,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        env.update({"TG_TEST_URL": origin, "TG_TEST_OWNER": settings.owner_token})
        if os.environ.get("TG_TEST_SCREENSHOT"):
            env["TG_SCREENSHOT"] = str(tmp_path / "admin.png")
        process = await asyncio.create_subprocess_exec(
            "node",
            "tests/browser/admin.cjs",
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, stderr.decode()
        assert b"Browser admin workflow passed" in stdout
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
        sock.close()
