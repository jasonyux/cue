"""Run one task with Claude Code through the Claude Agent SDK
"""

from __future__ import annotations

import asyncio
import shutil
import warnings
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError, ProcessError
from claude_agent_sdk.types import CanUseToolShadowedWarning

# The run's own MCP servers are allowed whole, so the person is never asked about them: what the SDK warns of is meant.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

from ....errors import RunnerCoreError
from ....processes import await_active
from ..prompts import ActorInput, instructions
from .approvals import WAITING, Approvals
from .browser import OPEN
from .events import Messages
from .servers import FOLDERS, NAMES, mcp_servers

FILES = {"instructions": "instructions.md", "errors": "stderr.log"}
# The backend's Anthropic key set is for its own models; inherited, it would put Claude Code on that key or endpoint
# instead of the user's login. Every Claude Code the backend starts gets them empty, which counts as unset.
ON_LOGIN = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": ""}


def cli() -> str:
    """The Claude Code a run uses: the one installed (and signed in), else the SDK's bundled copy, which reads the same login."""
    return shutil.which("claude") or str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


def options(detail: dict[str, Any], inputs: ActorInput, folder: Path, approvals: Approvals, errors: Any) -> ClaudeAgentOptions:
    """How Claude Code runs this task: the actor's instructions on its system prompt, the run's MCP servers and only
    those, allowed without asking, the person as its permission prompt, the session's model and permission mode when
    set, and the previous turn resumed for a chat turn."""
    return ClaudeAgentOptions(
        cli_path=cli(),
        cwd=str(Path(detail.get("actorClaudeCwd") or detail["workspacePath"]).expanduser()),
        system_prompt={"type": "preset", "preset": "claude_code", "append": instructions(inputs)},
        setting_sources=["user", "project", "local"],
        mcp_servers=mcp_servers(detail, folder),
        # strict_mcp_config=True,
        strict_mcp_config=False,  # allow connector tools
        allowed_tools=[f"mcp__{name}" for name in NAMES],
        can_use_tool=approvals.ask,
        max_buffer_size=32 * 1024 * 1024,  # a tool result with a cell's images, past the SDK's 1 MB
        model=detail.get("actorClaudeModel"),
        permission_mode=detail.get("actorClaudePermissionMode"),
        resume=(detail.get("chat") or {}).get("resume"),  # a later chat turn continues Claude Code's own conversation
        env=ON_LOGIN,
        stderr=lambda line: errors.write(line + "\n"),
    )


async def models() -> list[dict[str, Any]]:
    """The models Claude Code offers this login, as its own model picker lists them; "default" is Claude Code's choice."""
    async with ClaudeSDKClient(options=ClaudeAgentOptions(cli_path=cli(), setting_sources=[], strict_mcp_config=True, env=ON_LOGIN)) as client:
        info = await client.get_server_info()
    if info is None:
        raise RunnerCoreError("Claude Code did not say which models it offers.", code="agent_unavailable", status_code=503)
    return [{"id": model["value"], "label": model["displayName"], "detail": model.get("description") or "", "default": model["value"] == "default"}
            for model in info["models"]]


async def run_claude_agent(context: Any, inputs: ActorInput) -> dict[str, Any]:
    """Start Claude Code on the run's task and relay what it does until it ends."""
    detail = context.detail
    run_id = detail["run"]["id"]
    folder = context.screenshot_directory.parent / "claude"
    folder.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread((folder / FILES["instructions"]).write_text, f"{instructions(inputs)}\n", encoding="utf-8")  # for the record
    events = Messages(context)
    events.browser = OPEN[detail["actorBrowserEndpoint"]]  # the monitor follows the tab the agent is on; the run keeps a frame of it after each browser step
    events.frames = folder / FOLDERS["frames"]  # and one of the Mac after each step there, left by the desktop server
    WAITING[run_id] = approvals = Approvals(events)
    exit_code = "unknown"
    with (folder / FILES["errors"]).open("a", encoding="utf-8") as errors:
        try:
            async with ClaudeSDKClient(options=options(detail, inputs, folder, approvals, errors)) as client:
                await events.emit("run_progress", "ok", "Claude Code started.", str(Path(detail.get("actorClaudeCwd") or detail["workspacePath"]).expanduser()))
                await client.query(inputs.task.strip())
                await await_active(relay(client, events), context.signal)
        except CLINotFoundError as error:
            raise RunnerCoreError("Claude Code was not found.", code="agent_unavailable",
                                  hint="Install Claude Code and sign in, or reinstall the backend (the SDK bundles a copy).") from error
        except ProcessError as error:  # Claude exited without a result: not signed in, a bad option
            exit_code = str(error.exit_code)
            errors.write((error.stderr or "") + "\n")
        finally:
            WAITING.pop(run_id, None)
            if detail.get("chat") and events.session_id:  # whatever the turn's outcome, the next turn can resume it
                detail["chat"]["claudeSessionId"] = events.session_id
    errors_tail = (folder / FILES["errors"]).read_text(encoding="utf-8", errors="replace").strip()[-400:]
    return await events.outcome(exit_code, errors_tail)


async def relay(client: ClaudeSDKClient, events: Messages) -> None:
    """Every message Claude sends, to the run, until its result."""
    async for message in client.receive_response():
        await events.handle(message)
