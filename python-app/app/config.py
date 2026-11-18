import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key
from ruamel.yaml import YAML

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
_defaults = json.loads((REPO_ROOT / "contracts" / "defaults.json").read_text(encoding="utf-8"))
DEFAULT_MODEL: str = _defaults["model"]
DEFAULT_MODEL_DISPLAY_NAME: str = _defaults["modelDisplayName"]


# Two files at the repo root, nothing in both: .env holds the providers' keys and base URLs (and whatever else the backend
# reads from its environment), settings.yaml every other setting of a session (app/live/config.py).
ENV_FILE = REPO_ROOT / ".env"
SETTINGS_FILE = REPO_ROOT / "settings.yaml"
SHELL = frozenset(os.environ)  # the variables the backend started with (the launcher passes only the shell's): they win over .env
_yaml = YAML()  # round-trip: settings.yaml keeps its comments and order through a save
_yaml.representer.add_representer(type(None), lambda representer, _: representer.represent_scalar("tag:yaml.org,2002:null", "null"))  # unset: `null`, not a blank


def load_environment() -> None:
    """Read .env into the environment. Called again after the settings page saves or opens, so a file edited by hand
    applies to the next session without a restart."""
    for key, value in dotenv_values(ENV_FILE).items():
        if key not in SHELL and value is not None:
            os.environ[key] = value


def save_environment(values: dict[str, str]) -> None:
    """Write variables to .env, then read it again."""
    for key, value in values.items():
        set_key(ENV_FILE, key, value, export=True)
    load_environment()


def load_settings() -> dict[str, Any]:
    """settings.yaml as its nested mapping (with its comments, for a save); empty without the file."""
    data = _yaml.load(SETTINGS_FILE) if SETTINGS_FILE.exists() else None
    if data is not None and not isinstance(data, dict):
        raise ValueError(f"{SETTINGS_FILE.name} must be a mapping of sections.")
    return {} if data is None else data


def save_settings(data: dict[str, Any]) -> None:
    """Write the mapping back as settings.yaml."""
    _yaml.dump(data, SETTINGS_FILE)


@dataclass(frozen=True)
class Settings:
    data_root: Path = APP_ROOT / "data"
    default_model: str = DEFAULT_MODEL
    host: str = "127.0.0.1"
    port: int = 4041

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(data_root=Path(os.environ.get("CUA_DATA_ROOT", APP_ROOT / "data")).resolve(),
                   default_model=os.environ.get("CUA_DEFAULT_MODEL", DEFAULT_MODEL),
                   host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "4041")))

    def capabilities(self) -> dict:
        return {"backendId": "python", "codeTool": "exec_py", "browserModes": ["headful"],
                "defaults": {"browserMode": "headful", "model": self.default_model,
                             "maxResponseTurns": 24}}
