from __future__ import annotations

import pytest

from tests.helpers import Clock


@pytest.fixture(autouse=True)
def no_archive(monkeypatch):
    """No test asks the real Wayback Machine: one that wants an archive sets app.archive."""
    monkeypatch.setattr("scout.app.make_archive", lambda spec, fetcher: None)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated Scout: its own settings file, data folder and exchange folder."""
    env_file = tmp_path / "scout.env"
    env_file.write_text(
        f"SCOUT_DATA_DIR={tmp_path / 'data'}\nSCOUT_LLM=exchange:{tmp_path / 'exchange'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCOUT_ENV_FILE", str(env_file))
    for key in ("SCOUT_LLM", "SCOUT_DATA_DIR", "SCOUT_OUTPUT_DIR", "LM_STUDIO_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path
