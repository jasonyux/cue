"""The agent's browser: cmux's built-in browser, in one workspace per session that the person can watch.

A turn's own workspace closes with the turn, so a page opened there would vanish before the person could look, let
alone log in. Instead there is one workspace titled "agent browser": the session finds it (left by an earlier session
or a restart) or opens it on its first cmux run, and closes it when it stops, so there is never more than one. The
launcher hands its ref to Claude as ``CUA_ACTOR_CMUX_BROWSER_WORKSPACE``, and the agent's instructions say to open pages there
(``cmux new-pane --type browser --url … --workspace $CUA_ACTOR_CMUX_BROWSER_WORKSPACE --profile agent``). Pages stay open between
turns, the person can step in there by hand, and after every browser step the run gets a screenshot of each page in
it (``snapshots``), so the run card shows what the agent saw.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any

from .cli import cmux

TITLE = "agent browser"


async def workspaces() -> list[dict[str, Any]]:
    """Every workspace cmux has, as ``cmux tree`` lists them (ref, title, panes with their surfaces)."""
    tree = json.loads(await cmux("tree", "--all", "--json"))
    return [found for window in tree.get("windows") or [] for found in window.get("workspaces") or []]


async def workspace() -> str:
    """The agent browser workspace's ref: the one titled so if it exists, else a new one with nothing running in it,
    so it stays open."""
    for found in await workspaces():
        if found.get("title") == TITLE:
            return str(found["ref"])
    created = await cmux("rpc", "workspace.create", json.dumps({"title": TITLE, "focus": False}))
    return str(json.loads(created)["workspace_ref"])


async def pages(workspace: str) -> list[dict[str, Any]]:
    """The browser surfaces in the workspace: ref, title and url of each."""
    return [
        {"ref": surface["ref"], "title": surface.get("title") or "", "url": surface.get("url") or ""}
        for found in await workspaces()
        if found.get("ref") == workspace
        for pane in found.get("panes") or []
        for surface in pane.get("surfaces") or []
        if surface.get("type") == "browser"
    ]


async def snapshots(workspace: str) -> list[dict[str, Any]]:
    """A PNG of every page in the workspace, as base64, with its title and url; nothing when there is no page."""
    shots = []
    for page in await pages(workspace):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.png"
            await cmux("browser", "--surface", page["ref"], "screenshot", "--out", str(path), "--json")
            shots.append({**page, "base64": base64.b64encode(path.read_bytes()).decode()})
    return shots


def is_browser_step(command: str) -> bool:
    """Whether a shell command the agent ran touched cmux's browser: when the pages are worth a screenshot."""
    return "cmux browser" in command or "--type browser" in command
