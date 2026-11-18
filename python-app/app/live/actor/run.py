"""The actor's entry point: one task run from the live page, by the agent the run names.

The runner hands every live run to ``run_task`` (``scenario_runtime.execute_scenario`` calls it and does nothing else
for such a run). It builds the ``ActorInput`` in ``prompts.py`` from the run and what the session showed it, keeps the
live context with the run as ``live_context.md``, and hands the input to the agent: the desktop agent (``desktop/``,
an OpenAI model driving the desktop worker), the cmux agent (``cmux/``, Claude Code in a cmux workspace), or the claude
and codex agents (``standalone/``, Claude Code and Codex started by the backend). Each agent returns the final message and
the run's notes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...errors import RunnerCoreError
from ...scenario_runtime import save_text
from ..config import agent_rules
from .cmux.run import run_cmux_agent
from .desktop.run import run_desktop_agent
from .prompts import ActorInput
from .standalone.claude import run_claude_agent
from .standalone.codex import run_codex_agent


def permissions_text(context: Any) -> str:
    """The person's rules as the run is told them: for an agent with ax on the Mac, read from the session's folder now; "" otherwise."""
    folder = context.detail.get("actorSessionFolder")
    if not folder or "local" not in context.desktop_source or context.detail["actorAgent"] == "cmux":
        return ""
    return agent_rules(Path(folder)).current().describe()


async def run_task(context: Any) -> None:
    """Build the run's input, run it on its agent, and complete the run."""
    run = context.detail["run"]
    if run["labId"] != "desktop":
        raise RunnerCoreError(f"A run from the live page uses the desktop scenario, not {run['scenarioId']}.", code="unsupported_scenario",
                              status_code=400)
    inputs = ActorInput(
        agent=context.detail["actorAgent"],
        task=run["prompt"],
        input=context.actor_inputs,  # None for a chat turn: its prompt carries the context
        desktops=context.desktop_source,
        permissions=permissions_text(context),
    )
    await asyncio.to_thread(save_text, context.screenshot_directory.parent / "live_context.md", context.detail["liveContext"])
    if inputs.agent == "cmux":
        result = await run_cmux_agent(context, inputs)
    elif inputs.agent == "claude":
        result = await run_claude_agent(context, inputs)
    elif inputs.agent == "codex":
        result = await run_codex_agent(context, inputs)
    else:
        result = await run_desktop_agent(context, inputs)
    await context.complete_run(notes=result["notes"])
