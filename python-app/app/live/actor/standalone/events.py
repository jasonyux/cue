"""Claude Code's messages, as the SDK hands them over → the run events and usage the console already renders.

The cmux agent's ``cmux/events.py`` does the same from the raw stream lines; this is its counterpart for the SDK's
typed messages, without what is cmux's own. The session's ``init`` names the model and the tool servers; each
``AssistantMessage`` carries text, reasoning or tool calls with its usage; each ``UserMessage`` carries tool results;
the ``ResultMessage`` closes with the final answer, the token totals per model and the cost. Permission requests
never come this way: they are the callback in ``approvals.py``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from PIL import Image

from ....processes import await_active
from ....usage import COUNTERS, empty_counts, estimate_cost

DETAIL_LIMIT = 4000  # characters of one text block or tool result kept on an event
TOKEN_FIELDS = {"input": "input_tokens", "cacheRead": "cache_read_input_tokens", "cacheWrite": "cache_creation_input_tokens",
                "output": "output_tokens"}  # our count → the field of an assistant message's usage block


class Surface:
    """What the runner files a screenshot under: where it came from."""

    mode = "headful"
    target_label = "Claude Code"
    viewport: ClassVar[dict[str, int]] = {"width": 1, "height": 1}  # every screenshot carries its own size

    async def read_state(self) -> dict[str, str]:
        return {"currentUrl": "claude://session"}


def as_png(data: str) -> dict[str, Any]:
    """A tool result's image (base64, any format) → what the runner stores: PNG bytes and their size."""
    return png_of(base64.b64decode(data))


def png_of(raw: bytes) -> dict[str, Any]:
    image = Image.open(io.BytesIO(raw))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return {"base64": base64.b64encode(buffer.getvalue()).decode(), "width": image.width, "height": image.height}


def frames_left(folder: Path) -> list[dict[str, Any]]:
    """The frames of the Mac the desktop server left since the last look (``desktop_server.py``), oldest first, as the
    runner stores them; the files are taken."""
    shots = []
    for path in sorted(folder.glob("*.png")) if folder.is_dir() else []:
        shots.append(png_of(path.read_bytes()))
        path.unlink()
    return shots


TOTALS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "costUSD")


def this_turn(totals: Mapping[str, Mapping[str, Any]], before: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """A resumed session's totals less what it had reported by the end of the previous turn: Claude Code counts a
    resumed session from its start, so a chat turn would otherwise carry every earlier turn's tokens and cost again.
    A model whose totals went down (a session counted afresh) keeps its own; one with nothing new is left out."""
    turn = {}
    for model, counts in totals.items():
        prior = before.get(model) or {}
        if any(counts.get(key, 0) < prior.get(key, 0) for key in TOTALS):
            prior = {}
        rest = {**counts, **{key: counts.get(key, 0) - prior.get(key, 0) for key in TOTALS}}
        if any(rest[key] for key in TOTALS):
            turn[model] = rest
    return turn


def usage_wire(models: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``run["usage"]`` from per-model counts. Claude counts cached tokens apart from the input; our counters include them.

    A model's cost is the one Claude reported (``costUsd``) when it did, else the price table's estimate.
    """
    total: dict[str, Any] = {**empty_counts(), "byModel": []}
    for model, counts in models.items():
        entry = {
            "model": model,
            **empty_counts(),
            "calls": counts["calls"],
            "inputTokens": counts["input"] + counts["cacheRead"] + counts["cacheWrite"],
            "cachedInputTokens": counts["cacheRead"],
            "cacheWriteTokens": counts["cacheWrite"],
            "outputTokens": counts["output"],
        }
        entry["totalTokens"] = entry["inputTokens"] + entry["outputTokens"]
        cost = counts["costUsd"] if "costUsd" in counts else estimate_cost(model, entry)
        if cost is not None:
            entry["costUsd"] = round(cost, 6)
        for key in COUNTERS:
            total[key] += entry[key]
        total["byModel"].append(entry)
    if all("costUsd" in entry for entry in total["byModel"]):
        total["costUsd"] = round(sum(entry["costUsd"] for entry in total["byModel"]), 6)
    return total


class Messages:
    """Folds one run's messages into events on its context, and its usage onto the run record."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.surface = Surface()
        self.turn = 0
        self.seen: set[str] = set()  # assistant message ids already counted as a model call
        self.tools: dict[str, str] = {}  # tool call id → tool name, for the result that answers it
        self.session_id = ""  # Claude's session, from its first message; a later chat turn resumes it
        self.models: dict[str, dict[str, Any]] = {}  # model → calls and tokens so far, counted from the messages
        self.result: ResultMessage | None = None  # the closing message; None while Claude is still working
        self.browser: Any = None  # the session's agent browser (browser.py): told each browser tool result, and asked for the frame after it
        self.frames: Path | None = None  # where the desktop server leaves a frame of the Mac after each step there

    async def emit(self, kind: str, level: str, message: str, detail: str | None = None) -> None:
        event = {"type": kind, "level": level, "message": message, **({"detail": detail[:DETAIL_LIMIT]} if detail else {})}
        await await_active(self.context.emit_event(event), self.context.signal)

    async def handle(self, message: Any) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            await self.started(message.data)
        elif isinstance(message, AssistantMessage):
            await self.assistant(message)
        elif isinstance(message, UserMessage):
            await self.tool_results(message)
        elif isinstance(message, ResultMessage):
            self.finished(message)

    async def started(self, data: dict[str, Any]) -> None:
        """The session's first message: its model goes on the run record; a tool server that did not connect is a
        warning, and the run goes on without its tools."""
        self.context.detail["run"]["model"] = data["model"]
        self.session_id = data.get("session_id") or ""
        servers = data.get("mcp_servers") or []
        listed = ", ".join(f"{server['name']} {server['status']}" for server in servers)
        await self.emit(
            "run_progress",
            "ok",
            "Claude Code session started.",
            f"{data['model']} · permission mode {data.get('permissionMode')} · {len(data.get('tools') or [])} tools · {listed}",
        )
        for server in servers:
            if server["status"] != "connected":
                await self.emit("run_progress", "warn", f"Claude Code started without its {server['name']} tools.",
                                f"The {server['name']} MCP server is {server['status']}. Test configuration checks what it needs.")

    async def assistant(self, message: AssistantMessage) -> None:
        if message.message_id and message.message_id not in self.seen:  # one reply arrives as a message per content block, all with its id and usage
            self.seen.add(message.message_id)
            self.turn += 1
            usage = message.usage or {}
            counts = self.models.setdefault(message.model, {"calls": 0, **{key: 0 for key in TOKEN_FIELDS}})
            counts["calls"] += 1
            for key, field in TOKEN_FIELDS.items():
                counts[key] += int(usage.get(field) or 0)
            self.context.detail["run"]["usage"] = usage_wire(self.models)
            sent = sum(int(usage.get(TOKEN_FIELDS[key]) or 0) for key in ("input", "cacheRead", "cacheWrite"))
            await self.emit(
                "run_progress",
                "ok",
                f"Claude turn {self.turn} completed.",
                f"{message.message_id} · {sent} in · {int(usage.get('output_tokens') or 0)} out · 0 reasoning",
            )
        for block in message.content:
            if isinstance(block, TextBlock):
                await self.emit("run_progress", "pending", "Model progress.", block.text.strip())
            elif isinstance(block, ThinkingBlock):
                await self.emit("run_progress", "pending", "Model reasoning.", block.thinking.strip())
            elif isinstance(block, ToolUseBlock):
                self.tools[block.id] = block.name
                await self.emit("function_call_requested", "pending", "Function tool call received from the model.",
                                f"{block.name} {json.dumps(block.input)}")

    async def tool_results(self, message: UserMessage) -> None:
        for block in message.content if isinstance(message.content, list) else []:
            if not isinstance(block, ToolResultBlock):
                continue
            name = self.tools.get(block.tool_use_id, "tool")
            parts: Any = block.content or []
            if isinstance(parts, str):  # a plain-text result
                parts = [{"type": "text", "text": parts}]
            for part in parts:
                if part["type"] == "text" and part["text"].strip():
                    await self.emit("tool_progress", "pending", f"{name} output.", part["text"].strip())
                    if name.startswith("mcp__browser__") and self.browser is not None:
                        self.browser.seen(part["text"])
                        shot = await self.browser.frame()  # the page after the step, for the run's record; the model reads the text
                        if shot:
                            await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
                elif part["type"] == "image":
                    shot = await asyncio.to_thread(as_png, part["source"]["data"])
                    await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
            if name.startswith("mcp__desktop__") and self.frames is not None:
                for shot in await asyncio.to_thread(frames_left, self.frames):  # the Mac after the step, for the run's record
                    await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
            await self.emit("function_call_completed", "warn" if block.is_error else "ok", "Function tool call completed.", name)

    def finished(self, message: ResultMessage) -> None:
        """Claude's own totals replace the counts from the messages: they are exact, and they carry the cost. A resumed
        chat turn's totals are the whole Claude session's, so what the session had reported when the previous turn
        ended (``resumeTotals``) is taken off, and the new totals go on the chat block for the next turn."""
        self.result = message
        chat = self.context.detail.get("chat")
        totals = message.model_usage or {}
        if chat is not None and totals:
            chat["claudeTotals"] = totals  # the next turn, resuming this session, takes these off its own
        models = {
            model: {
                "calls": self.models.get(model, {}).get("calls", 0),
                "input": counts["inputTokens"],
                "cacheRead": counts["cacheReadInputTokens"],
                "cacheWrite": counts["cacheCreationInputTokens"],
                "output": counts["outputTokens"],
                "costUsd": counts["costUSD"],
            }
            for model, counts in this_turn(totals, (chat or {}).get("resumeTotals") or {}).items()
        }
        if models:  # a result that ended in an error may carry none; the message counts stay then
            self.context.detail["run"]["usage"] = usage_wire(models)

    async def outcome(self, exit_code: str, errors: str) -> dict[str, Any]:
        """What the run returns once Claude has ended: its final answer and the run's notes. Ending with no result
        (``errors``: the tail of its stderr) or with an error fails the run."""
        result = self.result
        if result is None:
            raise RuntimeError(f"Claude Code exited with code {exit_code} and no result. {errors}")
        if result.is_error or result.subtype != "success":
            raise RuntimeError(f"Claude Code ended with {result.subtype}: {result.result or 'no final message'}")
        final = result.result or ""
        await self.emit("run_progress", "ok", "Model returned a final response.", final)
        if self.context.detail.get("chat"):
            self.context.detail["chat"]["reply"] = final  # the turn's reply, as the chat shows it
        return {"final_assistant_message": final, "notes": ["Executed the task with Claude Code.", f"Model final response: {final}"]}
