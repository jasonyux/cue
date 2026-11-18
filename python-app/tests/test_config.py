import os

import pytest

from app import config


def test_root_env_load_happens_with_shell_precedence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('OPENAI_API_KEY="file-key"\nCUA_DEFAULT_MODEL="file-model"\nPORT="4141"\n')
    app_root = tmp_path / "python-app"
    app_root.mkdir()
    (app_root / ".env").write_text('PORT="4242"\n')
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.yaml")
    monkeypatch.setattr(config, "APP_ROOT", app_root)
    monkeypatch.setattr(config, "SHELL", frozenset({"OPENAI_API_KEY", "CUA_DEFAULT_MODEL"}))  # what the backend started with
    monkeypatch.setenv("OPENAI_API_KEY", "shell-key")
    monkeypatch.setenv("CUA_DEFAULT_MODEL", "shell-model")
    monkeypatch.delenv("PORT", raising=False)
    config.load_environment()
    assert os.environ["OPENAI_API_KEY"] == "shell-key"
    assert config.Settings.from_environment().default_model == "shell-model"
    assert config.Settings.from_environment().port == 4141


def test_app_env_is_not_a_fallback(tmp_path, monkeypatch):
    app_root = tmp_path / "python-app"
    app_root.mkdir()
    (app_root / ".env").write_text('OPENAI_API_KEY="app-key"\nPORT="4242"\n')
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.yaml")
    monkeypatch.setattr(config, "APP_ROOT", app_root)
    monkeypatch.setattr(config, "SHELL", frozenset())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    config.load_environment()
    assert "OPENAI_API_KEY" not in os.environ
    assert config.Settings.from_environment().port == 4041


def test_saving_writes_the_env_file_and_reads_it_again(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("# mine\nexport PORT=4343\n")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.yaml")
    monkeypatch.setattr(config, "SHELL", frozenset({"PORT"}))
    for name in ("PORT", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "")  # restored afterwards, whatever the file sets
    config.save_environment({"OPENAI_API_KEY": "sk-1", "PORT": "4444"})
    assert (tmp_path / ".env").read_text() == "# mine\nexport PORT='4444'\nexport OPENAI_API_KEY='sk-1'\n"
    assert (os.environ["OPENAI_API_KEY"], os.environ["PORT"]) == ("sk-1", "")  # the shell's wins


def test_the_settings_file_is_a_nested_mapping_or_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.yaml")
    assert config.load_settings() == {}
    config.save_settings({"proposer": {"model": "m", "inputs": ["tasks"], "thinking": False}, "summary": {"base_url": None}})
    assert (tmp_path / "settings.yaml").read_text() == "proposer:\n  model: m\n  inputs:\n  - tasks\n  thinking: false\nsummary:\n  base_url: null\n"
    assert config.load_settings() == {"proposer": {"model": "m", "inputs": ["tasks"], "thinking": False}, "summary": {"base_url": None}}
    (tmp_path / "settings.yaml").write_text("# mine\nproposer:\n  model: m  # it\n")
    data = config.load_settings()
    data["proposer"]["model"] = "m2"
    config.save_settings(data)  # a hand-written file keeps its comments
    assert (tmp_path / "settings.yaml").read_text().startswith("# mine\nproposer:\n  model: m2 ") and "# it" in (tmp_path / "settings.yaml").read_text()
    (tmp_path / "settings.yaml").write_text("- not a mapping\n")
    with pytest.raises(ValueError, match="settings.yaml"):
        config.load_settings()
