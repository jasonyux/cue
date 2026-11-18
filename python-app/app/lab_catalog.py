import copy
import json
import sys
from functools import lru_cache
from pathlib import Path

from .config import DEFAULT_MODEL_DISPLAY_NAME
from .models import ScenarioManifest, validate_wire

AX_ROOT = Path(__file__).resolve().parent / "desktop" / "ax"


@lru_cache(maxsize=1)
def desktop_catalog() -> dict:
    """The scenarios: each operates local macOS apps."""
    return json.loads((AX_ROOT / "scenarios.json").read_text(encoding="utf-8"))


def definitions() -> list[dict]:
    return list(desktop_catalog()["scenarios"]) if sys.platform == "darwin" else []


def list_scenarios() -> list[dict]:
    return [validate_wire(ScenarioManifest, {**item, "workspaceTemplatePath": str(AX_ROOT / item["templateDirectory"])}) for item in definitions()]


def instructions(scenario_id: str, *, mode: str = "pyautogui") -> str:
    definition = next(item for item in definitions() if item["id"] == scenario_id)
    if mode == "accessibility":
        preamble = [
            (
                f"You are operating local macOS apps through the Accessibility API for a {DEFAULT_MODEL_DISPLAY_NAME} CUA demo harness. "
                "Nothing you do moves the user's mouse or steals keyboard focus; the user keeps working on the same Mac."
            ),
            "You must use the exec_py tool before you answer.",
            (
                "Observe with state = ax.get_state(app); log(state.text), and display(state.screenshot) when the text is not enough. "
                "Act on element indexes from the latest state, then re-read the state."
            ),
            "Pick or launch the app the task needs with ax.get_state(name) or ax.launch(name).",
        ]
    else:
        preamble = [
            f"You are operating a persistent Python/PyAutoGUI desktop session for a {DEFAULT_MODEL_DISPLAY_NAME} CUA demo harness.",
            "You must use the exec_py tool before you answer.",
            "Observe the current interface with pyautogui.screenshot(), then use PyAutoGUI mouse and keyboard controls. Coordinates refer to the full desktop screenshot.",
            "Open apps with subprocess.run(['open', '-a', name]), then observe with pyautogui.screenshot().",
        ]
    return "\n".join([*preamble, *copy.deepcopy(definition["instructions"])])
