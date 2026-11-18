"""The tools a standalone agent (Claude Code, Codex) gets beyond its own: MCP servers started for the run, the same for
every agent.

- ``desktop``: the session's desktops as exec_py (``desktop_server.py``): the user's Mac through the Accessibility API
  (``ax``, in the background: it never moves the user's mouse or takes their keyboard focus), the remote desktop, or
  both. Which, and where the remote one is, the server is told in its environment; the remote desktop's token it
  inherits from the backend's, never from a file (Claude Code passes its environment on; Codex starts a server with a
  bare one and is told to forward the token by name, ``env_vars`` in ``codex.py``).
- ``browser``: Playwright's MCP server, attached to the session's agent browser (``browser.py``: headless, on a profile
  that keeps logins between sessions, watched and driven by the person from the monitor). It runs with npx, so it
  needs Node.
- ``history``: search_history (``history_server.py``), the history index over the stream (``storage/search.py``), the
  same tool the proposer has: what the person did before, from every session, for a detail of the task the agent is
  unsure of. It is told the backend's data root and the run's start: its results stay before that, so the run's own
  question, on the stream as the run starts, is not among them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ....config import Settings
from ..desktop.tools import session_env

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"
NAMES = ("desktop", "browser", "history")
FOLDERS = {"browser": "browser", "frames": "frames"}  # under the run's agent folder: the browser server's snapshots and files, the desktop server's frames of the Mac


def mcp_servers(detail: dict[str, Any], folder: Path) -> dict[str, Any]:
    """The servers of one run, as an MCP configuration lists them (stdio, each a command): the desktop server told the
    run's desktops (``actorDesktopSource``, and the remote one's provider and URL) and where to leave a frame of the Mac
    after each step there (``folder/frames``, which the relay files), the browser server attached to the session's agent
    browser (``actorBrowserEndpoint``) and saving this run's page snapshots and files in ``folder/browser``, the history
    server told the data root and the run's start."""
    desktop_env = {"CUA_ACTOR_DESKTOP_SOURCE": ",".join(detail["actorDesktopSource"]), "CUA_DESKTOP_FRAMES": str(folder / FOLDERS["frames"]), **session_env(detail)}
    if detail.get("desktopRemoteProvider"):
        desktop_env |= {"CUA_DESKTOP_REMOTE_PROVIDER": detail["desktopRemoteProvider"], "CUA_DESKTOP_REMOTE_URL": detail["desktopRemoteUrl"]}
    return {
        "desktop": {"command": sys.executable, "args": ["-m", "app.live.actor.standalone.desktop_server"], "env": desktop_env},
        "browser": {"command": "npx", "args": ["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", detail["actorBrowserEndpoint"], "--output-dir", str(folder / FOLDERS["browser"])]},
        "history": {"command": sys.executable, "args": ["-m", "app.live.actor.standalone.history_server"],
                    "env": {"CUA_DATA_ROOT": str(Settings.from_environment().data_root), "CUA_HISTORY_BEFORE": detail["run"]["startedAt"]}},
    }
