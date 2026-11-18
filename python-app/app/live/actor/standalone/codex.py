"""Run one task with Codex through OpenAI's Codex SDK (``openai-codex``)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import tomllib
from openai_codex import ApprovalMode, AsyncCodex, AsyncTurnHandle, CodexConfig, CodexError, Sandbox
from pydantic import BaseModel

from ....errors import RunnerCoreError
from ....processes import await_active
from ....usage import COUNTERS, empty_counts, estimate_cost
from ..prompts import ActorInput, instructions
from . import events
from .browser import OPEN
from .servers import FOLDERS, mcp_servers

FILES = {"instructions": "instructions.md"}
DETAIL_LIMIT = events.DETAIL_LIMIT
# FEATURES_OFF = ("plugins", "apps", "computer_use", "browser_use", "browser_use_external", "in_app_browser")  # Codex switches off for a run
FEATURES_OFF = ("computer_use", "browser_use", "browser_use_external", "in_app_browser")  # Codex switches off for a run
PLUGINS_OFF = ("unified-computer-use@openai-bundled", "computer-use@openai-bundled", "browser@openai-bundled")  # Codex's bundled computer use and in-app browser, whose REPL and skills the switches leave on: ours stand in


class Surface(events.Surface):
    target_label = "Codex"


def launch_overrides() -> tuple[str, ...]:
    """Codex's config for a run (``-c`` overrides): the switches in ``FEATURES_OFF`` and the bundled plugins in ``PLUGINS_OFF``
    off, and each MCP server of the user's Codex config disabled, so the run's servers are the only ones."""
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    servers = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {}) if config.exists() else {}
    return (*(f"features.{name}=false" for name in FEATURES_OFF), *(f'plugins."{name}".enabled=false' for name in PLUGINS_OFF),
            *(f"mcp_servers.{name}.enabled=false" for name in servers))


def thread_options(detail: dict[str, Any], inputs: ActorInput, folder: Path) -> dict[str, Any]:
    """How Codex runs this task: the actor's instructions, the run's MCP servers approved up front, its folder, the
    session's sandbox and model, and Codex's own reviewer for any step beyond the sandbox."""
    servers = {name: {**server, "default_tools_approval_mode": "approve"} for name, server in mcp_servers(detail, folder).items()}
    if detail.get("desktopRemoteProvider"):  # Codex starts a server with a bare environment: the token is forwarded by name, never written anywhere
        servers["desktop"]["env_vars"] = ["CUA_DESKTOP_REMOTE_TOKEN"]
    return {
        "developer_instructions": instructions(inputs),
        "config": {"mcp_servers": servers},
        "cwd": str(Path(detail.get("actorCodexCwd") or detail["workspacePath"]).expanduser()),
        "sandbox": Sandbox(detail["actorCodexSandbox"]),
        "model": detail.get("actorCodexModel"),
        "approval_mode": ApprovalMode.auto_review,
    }


def text_of(parts: list[Any]) -> str:
    """A reasoning item's summary or content: plain strings, or parts that carry one in ``text``."""
    return "\n".join(part if isinstance(part, str) else str(part.get("text") or "") for part in parts).strip()


class Items:
    """Folds one turn's notifications into events on its context, and its usage onto the run record."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.surface = Surface()
        self.thread_id = ""  # the thread a later chat turn resumes
        self.model = ""
        self.counts = {**empty_counts()}  # this run's model calls and tokens, summed from each call's usage
        self.final = ""  # the agent's final answer, once given
        self.turn: dict[str, Any] | None = None  # the turn as Codex closed it; None while it is still working
        self.browser: Any = None  # the session's agent browser (browser.py): told each browser tool result, and asked for the frame after it
        self.frames: Path | None = None  # where the desktop server leaves a frame of the Mac after each step there

    async def emit(self, kind: str, level: str, message: str, detail: str | None = None) -> None:
        event = {"type": kind, "level": level, "message": message, **({"detail": detail[:DETAIL_LIMIT]} if detail else {})}
        await await_active(self.context.emit_event(event), self.context.signal)

    async def started(self, sandbox: str) -> None:
        self.context.detail["run"]["model"] = self.model
        await self.emit("run_progress", "ok", "Codex session started.", f"{self.model} · sandbox {sandbox} · thread {self.thread_id}")

    async def handle(self, method: str, data: dict[str, Any]) -> None:
        if method == "item/started":
            await self.item_started(data["item"])
        elif method == "item/completed":
            await self.item_completed(data["item"])
        elif method == "thread/tokenUsage/updated":
            await self.model_call(data["tokenUsage"]["last"])
        elif method == "item/autoApprovalReview/completed":
            await self.reviewed(data["review"])
        elif method == "turn/completed":
            self.turn = data["turn"]

    async def item_started(self, item: dict[str, Any]) -> None:
        """A step begins: a tool call, as the run card and the terminal show one (name, then its arguments as JSON)."""
        kind = item["type"]
        if kind == "mcpToolCall":
            call = f"mcp__{item['server']}__{item['tool']} {json.dumps(item.get('arguments') or {})}"
        elif kind == "commandExecution":  # the command as typed, not the shell that wraps it
            actions = item.get("commandActions") or []
            call = f"shell {json.dumps({'command': actions[0]['command'] if actions else item['command']})}"
        elif kind == "fileChange":
            call = f"apply_patch {json.dumps({'files': [change['path'] for change in item['changes']]})}"
        elif kind == "webSearch":
            call = f"web_search {json.dumps({'query': item['query']})}"
        else:
            return
        await self.emit("function_call_requested", "pending", "Function tool call received from the model.", call)

    async def item_completed(self, item: dict[str, Any]) -> None:
        kind = item["type"]
        if kind == "agentMessage":
            if item.get("phase") == "final_answer":
                self.final = item["text"]
            if item["text"].strip():
                await self.emit("run_progress", "pending", "Model progress.", item["text"].strip())
        elif kind == "reasoning":
            thought = text_of(item.get("summary") or []) or text_of(item.get("content") or [])
            if thought:
                await self.emit("run_progress", "pending", "Model reasoning.", thought)
        elif kind == "mcpToolCall":
            await self.tool_result(item)
        elif kind == "commandExecution":
            if (item.get("aggregatedOutput") or "").strip():
                await self.emit("tool_progress", "pending", "shell output.", item["aggregatedOutput"].strip())
            failed = item["status"] != "completed" or item.get("exitCode") not in (0, None)
            await self.emit("function_call_completed", "warn" if failed else "ok", "Function tool call completed.", "shell")
        elif kind in ("fileChange", "webSearch"):
            name = "apply_patch" if kind == "fileChange" else "web_search"
            await self.emit("function_call_completed", "ok" if item.get("status", "completed") == "completed" else "warn", "Function tool call completed.", name)

    async def tool_result(self, item: dict[str, Any]) -> None:
        """An MCP tool's result: its text as output (and, from the browser, where the agent is now), its images as the
        run's screenshots."""
        name = f"mcp__{item['server']}__{item['tool']}"
        for part in (item.get("result") or {}).get("content") or []:
            if part["type"] == "text" and part["text"].strip():
                await self.emit("tool_progress", "pending", f"{name} output.", part["text"].strip())
                if item["server"] == "browser" and self.browser is not None:
                    self.browser.seen(part["text"])
                    shot = await self.browser.frame()  # the page after the step, for the run's record; the model reads the text
                    if shot:
                        await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
            elif part["type"] == "image":
                shot = await asyncio.to_thread(events.as_png, part["data"])
                await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
        if item["server"] == "desktop" and self.frames is not None:
            for shot in await asyncio.to_thread(events.frames_left, self.frames):  # the Mac after the step, for the run's record
                await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
        if item.get("error"):
            await self.emit("tool_progress", "pending", f"{name} output.", item["error"]["message"])
        failed = item["status"] != "completed" or bool(item.get("error"))
        await self.emit("function_call_completed", "warn" if failed else "ok", "Function tool call completed.", name)

    async def model_call(self, last: dict[str, Any]) -> None:
        """One model call's tokens: counted on the run, and marked as a turn the run card groups the steps under."""
        counts = self.counts
        counts["calls"] += 1
        for ours, theirs in (("inputTokens", "inputTokens"), ("cachedInputTokens", "cachedInputTokens"), ("cacheWriteTokens", "cacheWriteInputTokens"),
                             ("outputTokens", "outputTokens"), ("reasoningTokens", "reasoningOutputTokens"), ("totalTokens", "totalTokens")):
            counts[ours] += int(last.get(theirs) or 0)
        entry = {"model": self.model, **counts}
        cost = estimate_cost(self.model, entry)  # Codex reports no cost: the price table's, when it prices the model
        if cost is not None:
            entry["costUsd"] = round(cost, 6)
        self.context.detail["run"]["usage"] = {**{key: counts[key] for key in COUNTERS}, "byModel": [entry],
                                               **({"costUsd": entry["costUsd"]} if "costUsd" in entry else {})}
        await self.emit("run_progress", "ok", f"Codex turn {counts['calls']} completed.",
                        f"{int(last.get('inputTokens') or 0)} in · {int(last.get('outputTokens') or 0)} out · {int(last.get('reasoningOutputTokens') or 0)} reasoning")

    async def reviewed(self, review: dict[str, Any]) -> None:
        """Codex's reviewer judged a step beyond the sandbox."""
        approved = review["status"] == "approved"
        await self.emit("run_progress", "ok" if approved else "warn", f"Codex's reviewer {review['status']} a step.",
                        f"risk {review.get('riskLevel') or 'unknown'} · {review.get('rationale') or ''}")

    async def outcome(self) -> dict[str, Any]:
        """What the run returns once the turn has ended: its final answer and the run's notes. A turn that failed fails the run."""
        turn = self.turn
        if turn is None:
            raise RuntimeError("Codex closed before it finished its turn.")
        if turn["status"] != "completed":
            raise RuntimeError(f"Codex ended with {turn['status']}: {(turn.get('error') or {}).get('message') or 'no final message'}")
        await self.emit("run_progress", "ok", "Model returned a final response.", self.final)
        if self.context.detail.get("chat"):
            self.context.detail["chat"]["reply"] = self.final  # the turn's reply, as the chat shows it
        return {"final_assistant_message": self.final, "notes": ["Executed the task with Codex.", f"Model final response: {self.final}"]}


async def relay(turn: AsyncTurnHandle, items: Items) -> None:
    """Every notification of the turn, to the run, until it completes. One this SDK version does not model is skipped."""
    async for note in turn.stream():
        if isinstance(note.payload, BaseModel):
            await items.handle(note.method, note.payload.model_dump(mode="json", by_alias=True, exclude_none=True))


async def models() -> list[dict[str, Any]]:
    """The models Codex offers this login (the ones its own picker shows), its default marked."""
    async with AsyncCodex(CodexConfig(config_overrides=launch_overrides())) as codex:
        listed = await codex.models()
    return [{"id": model.id, "label": model.display_name, "detail": model.description, "default": model.is_default} for model in listed.data]


async def run_codex_agent(context: Any, inputs: ActorInput) -> dict[str, Any]:
    """Start Codex on the run's task (or resume the chat's thread) and relay what it does until the turn ends."""
    detail = context.detail
    folder = context.screenshot_directory.parent / "codex"
    folder.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread((folder / FILES["instructions"]).write_text, f"{instructions(inputs)}\n", encoding="utf-8")  # for the record
    items = Items(context)
    items.browser = OPEN[detail["actorBrowserEndpoint"]]  # the monitor follows the tab the agent is on; the run keeps a frame of it after each browser step
    items.frames = folder / FOLDERS["frames"]  # and one of the Mac after each step there, left by the desktop server
    options = thread_options(detail, inputs, folder)
    resume = (detail.get("chat") or {}).get("resume")  # a later chat turn continues Codex's own thread
    try:
        async with AsyncCodex(CodexConfig(cwd=options["cwd"], config_overrides=launch_overrides())) as codex:
            try:
                thread = await (codex.thread_resume(resume, **options) if resume else codex.thread_start(**options))
                items.thread_id = thread.id
                items.model = (await thread.read()).thread.model or detail["run"]["model"]  # the run record's, when Codex names none
            except CodexError as error:
                raise RunnerCoreError(f"Codex could not start: {error}", code="agent_unavailable",
                                      hint="Sign in to Codex once (codex login), then try again.") from error
            await items.started(detail["actorCodexSandbox"])
            turn = await thread.turn(inputs.task.strip())
            await await_active(relay(turn, items), context.signal)
    finally:
        if detail.get("chat") and items.thread_id:  # whatever the turn's outcome, the next turn can resume it
            detail["chat"]["codexThreadId"] = items.thread_id
    return await items.outcome()
