"""The desktop agent's entry point: the worker, the desktop session, then the model loop.

Given the run's ``ActorInput``, it starts the actor's worker on the user's Mac, the remote desktop, or both (the
session's ``actorDesktopSource``), opens the desktop session the screenshots come from, fills in the local worker's
mode, runs the Responses API loop in ``agent_loop.py`` with the tools in ``tools.py``, and closes everything when the
run ends. The run's screenshots and events are the same kinds as the scenario page's, so the console's run view and
replay read both.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .... import responses_loop
from ....desktop.ax.session import DesktopSession
from ....processes import assert_active
from ....scenario_runtime import close_resources, teardown
from ..prompts import ActorInput, instructions
from . import agent_loop, runtime
from . import remote as remote_desktops
from .tools import session_env


async def run_desktop_agent(context: Any, inputs: ActorInput) -> dict[str, Any]:
    """Run the task on the desktop worker(s) and return the final message and the run's notes."""
    run = context.detail["run"]
    client = responses_loop.create_default_responses_client()
    python: Any = None  # the worker on the user's Mac
    remote: Any = None  # the remote desktop, when the source includes it
    session: Any = None
    try:
        assert_active(context.signal)
        await context.emit_event({"type": "run_progress", "level": "ok", "detail": run["model"],
                                  "message": "Using the live Responses API code loop for the desktop lab."})
        if "local" in inputs.desktops:
            python = await runtime.launch_python_runtime(signal=context.signal, env=session_env(context.detail))
        if "remote" in inputs.desktops and context.desktop_remote is not None:
            remote = await remote_desktops.connect(*context.desktop_remote, signal=context.signal)
        primary = python if python is not None else remote  # the desktop the session captures its screenshots from
        mode = getattr(primary, "mode", "pyautogui")
        assert_active(context.signal)
        session = DesktopSession(
            runtime=primary,
            screenshot_dir=context.screenshot_directory,
            target_label=f"local macOS apps ({mode})" if python is not None else "remote desktop (pyautogui)",
            signal=context.signal,
        )
        session.remote = remote if python is not None else None  # a second desktop only next to the local one: exec_py takes a target
        await context.sync_browser_state(session)
        await context.emit_event({"type": "browser_session_started", "level": "ok", "detail": session.target_label,
                                  "message": "Desktop session ready. This scenario uses no lab page or browser."})
        await context.capture_screenshot(session, "desktop-start")
        result = await agent_loop.run_agent_loop(
            context=context,
            session=session,
            instructions=instructions(replace(inputs, mode=mode)),
            max_response_turns=run["maxResponseTurns"],
            client=client,
        )
        await context.capture_screenshot(session, "desktop-final")
        return result
    finally:
        await teardown(close_resources(python, remote, session, client))
