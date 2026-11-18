"""Run one task with Claude Code inside a cmux workspace.

cmux opens a background workspace whose process is ``launch.sh`` (the socket's ``initial_command``: the script runs in
place of the interactive shell, so nothing the shell's startup asks can hold it up). In a cmux workspace ``claude`` is
cmux's wrapper, which brings cmux's computer-use tools and reports permission requests to the Feed, so the agent can
work in the shell, on files, on the web and on the Mac's apps. The script runs ``claude -p`` headless on the run's
prompt, with the actor's instructions and the observed context appended to Claude Code's system prompt; its stream
goes to ``stream.jsonl`` in the run folder, and this module tails that file into run events (``events.py``). The
workspace closes itself when the script ends; stopping the run closes it, which also ends Claude.

cmux only accepts commands from processes started inside it, so the server has to be started from a cmux terminal.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

from ....errors import RunnerCoreError
from ....processes import await_active
from ..prompts import ActorInput, instructions
from .approvals import WAITING
from .cli import cmux
from .events import StreamEvents

POLL_S = 0.3  # how often the stream file is read
START_TIMEOUT_S = 60.0  # the workspace must have started the launcher by then
FILES = {
    "pid": "pid",
    "instructions": "instructions.md",
    "prompt": "prompt.md",
    "stream": "stream.jsonl",
    "errors": "stderr.log",
    "exit": "exit",
}
LAUNCHER = """#!/bin/bash
echo $$ > {pid}
export CUA_ACTOR_CMUX_BROWSER_WORKSPACE={browser}
claude -p --output-format stream-json --verbose --include-hook-events --max-turns {turns}{flags} --append-system-prompt-file {instructions} < {prompt} > {stream} 2> {errors}
echo $? > {exit}
"""


def write_launcher(folder: Path, detail: dict[str, Any], inputs: ActorInput) -> Path:
    """The run's prompt, the instructions appended to Claude Code's system prompt, and the script that runs Claude on them."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILES["prompt"]).write_text(f"{inputs.task.strip()}\n", encoding="utf-8")
    (folder / FILES["instructions"]).write_text(f"{instructions(inputs)}\n", encoding="utf-8")
    flags = "".join(
        f" {flag} {shlex.quote(detail[key])}"
        for flag, key in (("--model", "actorCmuxModel"), ("--permission-mode", "actorCmuxPermissionMode"))
        if detail.get(key)
    )
    if (detail.get("chat") or {}).get("resume"):  # a later chat turn continues Claude Code's own conversation
        flags += f" --resume {shlex.quote(detail['chat']['resume'])}"
    script = folder / "launch.sh"
    script.write_text(
        LAUNCHER.format(
            turns=detail["run"]["maxResponseTurns"],
            flags=flags,
            browser=shlex.quote(detail.get("actorCmuxBrowserWorkspace") or ""),
            **{name: shlex.quote(str(folder / file)) for name, file in FILES.items()},
        ),
        encoding="utf-8",
    )
    return script


def read_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """The complete lines written since ``offset``, parsed, and the offset after them."""
    if not path.exists():
        return [], offset
    with path.open("rb") as file:
        file.seek(offset)
        data = file.read()
    end = data.rfind(b"\n") + 1
    return [json.loads(line) for line in data[:end].splitlines() if line.strip()], offset + end


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def follow(context: Any, folder: Path, events: StreamEvents) -> None:
    """Relay the stream until the launcher has written its exit code."""
    offset = 0
    waited = 0.0
    while True:
        finished = (folder / FILES["exit"]).exists()  # looked up before reading, so every line written before it is read below
        lines, offset = await asyncio.to_thread(read_lines, folder / FILES["stream"], offset)
        for line in lines:
            await events.handle(line)
        if finished:
            return
        pid = (folder / FILES["pid"]).read_text().strip() if (folder / FILES["pid"]).exists() else ""
        if pid and not alive(int(pid)) and not (folder / FILES["exit"]).exists():
            raise RuntimeError("Claude Code stopped without finishing: its cmux workspace was closed or the process was killed.")
        if not pid and waited > START_TIMEOUT_S:
            raise RunnerCoreError(f"The cmux workspace did not start the launcher within {START_TIMEOUT_S:g} seconds.", code="cmux_unavailable")
        await await_active(asyncio.sleep(POLL_S), context.signal)
        waited += POLL_S


async def open_workspace(name: str, cwd: str, script: Path) -> str:
    """A background cmux workspace in ``cwd`` whose process is the launcher: its ref."""
    params = {"title": name, "cwd": cwd, "focus": False, "initial_command": f"bash {shlex.quote(str(script))}"}
    created = await cmux("rpc", "workspace.create", json.dumps(params))
    workspace = json.loads(created).get("workspace_ref")
    if not workspace:
        raise RunnerCoreError(f"cmux workspace.create answered {created!r}.", code="cmux_unavailable")
    return str(workspace)


async def close_workspace(workspace: str) -> None:
    """Closing the workspace ends a Claude that is still running; a workspace whose launcher has ended is already gone."""
    try:
        await cmux("workspace", "close", workspace)
    except RunnerCoreError as error:
        if "not_found" not in str(error):
            raise


async def run_cmux_agent(context: Any, inputs: ActorInput) -> dict[str, Any]:
    """Start Claude Code on the run's task in a new cmux workspace and relay what it does until it ends."""
    detail = context.detail
    folder = context.screenshot_directory.parent / "cmux"
    script = await asyncio.to_thread(write_launcher, folder, detail, inputs)
    cwd = str(Path(detail.get("actorCmuxCwd") or detail["workspacePath"]).expanduser())
    events = StreamEvents(context)
    workspace: str | None = None
    try:
        workspace = await await_active(open_workspace(f"run {detail['run']['id']}", cwd, script), context.signal)
        await events.emit("run_progress", "ok", "Claude Code launched in a cmux workspace.", f"{workspace} · {cwd}")
        await follow(context, folder, events)
    finally:
        WAITING.pop(detail["run"]["id"], None)
        if detail.get("chat") and events.session_id:  # whatever the turn's outcome, the next turn can resume it
            detail["chat"]["claudeSessionId"] = events.session_id
        if workspace is not None:
            await asyncio.shield(asyncio.ensure_future(close_workspace(workspace)))
    result = events.result
    if result is None:
        errors = (folder / FILES["errors"]).read_text(encoding="utf-8", errors="replace").strip()[-400:]
        raise RuntimeError(f"Claude Code exited with code {(folder / FILES['exit']).read_text().strip()} and no result. {errors}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise RuntimeError(f"Claude Code ended with {result.get('subtype')}: {result.get('result') or 'no final message'}")
    final = result.get("result") or ""
    await events.emit("run_progress", "ok", "Model returned a final response.", final)
    if detail.get("chat"):
        detail["chat"]["reply"] = final  # the turn's reply, as the chat shows it
    return {
        "final_assistant_message": final,
        "notes": ["Executed the task with Claude Code in a cmux workspace.", f"Model final response: {final}"],
    }
