"""Shared for the live tests."""

import itertools

import pytest

from app import config as app_config
from app.live.config import PROVIDER_KEYS
from app.live.storage import ops


@pytest.fixture(autouse=True)
def no_files(monkeypatch, tmp_path):
    """The live settings read settings.yaml and .env at the repo root, and the providers' key sets (OPENAI_*, VLLM_*, …) from
    the environment; every test starts without any of them, whatever the repo and the shell running the tests hold."""
    monkeypatch.setattr(app_config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_config, "SETTINGS_FILE", tmp_path / "settings.yaml")
    for name, _url in PROVIDER_KEYS.values():
        for suffix in ("BASE_URL", "API_KEY"):
            monkeypatch.delenv(f"{name}_{suffix}", raising=False)
    monkeypatch.delenv("CUA_DESKTOP_REMOTE_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def numbered_task_ids(monkeypatch):
    """Tasks get t001, t002, … in the order they are added, so tests can name them; real ids are random (ops.new_task_id)."""
    counter = itertools.count(1)
    monkeypatch.setattr(ops, "new_task_id", lambda: f"t{next(counter):03d}")
