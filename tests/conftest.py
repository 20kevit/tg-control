import pytest
from cryptography.fernet import Fernet

from tg_control.config import Settings
from tg_control.db import Base, make_engine, make_sessions


@pytest.fixture
def database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'synthetic.sqlite'}")
    Base.metadata.create_all(engine)
    yield make_sessions(engine)
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'synthetic.sqlite'}",
        owner_token="synthetic-owner-token-" + "x" * 32,
        encryption_key=Fernet.generate_key(),
        allowed_hosts=("testserver", "localhost", "127.0.0.1"),
        allowed_origins=("http://testserver",),
    )


@pytest.fixture
def api(settings, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from fastapi.testclient import TestClient

    from tg_control.api import create_app

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = settings.database_url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client
