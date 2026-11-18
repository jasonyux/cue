"""Accessibility control mode: operate macOS apps without touching the user's mouse or keyboard focus.

Enable it with ``CUA_DESKTOP_MODE=accessibility`` in the root ``.env``. The worker then exposes an ``ax``
global instead of ``pyautogui``. See ``client.py`` for the API the model uses and ``session.py`` for the
browser-less run session used by desktop scenarios.
"""

from .client import AccessibilityDesktop, AccessibilityError, AppState, RunningApp, frontmost_app, reactivate

MODE_ACCESSIBILITY = "accessibility"
MODE_PYAUTOGUI = "pyautogui"
MODE_ALIASES = {"ax": MODE_ACCESSIBILITY, "a11y": MODE_ACCESSIBILITY, MODE_ACCESSIBILITY: MODE_ACCESSIBILITY,
                MODE_PYAUTOGUI: MODE_PYAUTOGUI, "": MODE_PYAUTOGUI}


def desktop_mode(value: str | None) -> str:
    """Normalize the CUA_DESKTOP_MODE environment value."""
    key = (value or "").strip().lower()
    if key not in MODE_ALIASES:
        raise ValueError(f"CUA_DESKTOP_MODE must be 'pyautogui' or 'accessibility', not {value!r}.")
    return MODE_ALIASES[key]


__all__ = ["MODE_ACCESSIBILITY", "MODE_PYAUTOGUI", "AccessibilityDesktop", "AccessibilityError", "AppState",
           "RunningApp", "desktop_mode", "frontmost_app", "reactivate"]
