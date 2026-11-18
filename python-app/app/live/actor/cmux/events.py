"""Claude Code's stream lines → the run events and usage the console already renders.

``claude -p --output-format stream-json`` prints one JSON message per line: the session's ``init``, each ``assistant``
message (text, reasoning, tool calls), each ``user`` message carrying tool results, and a closing ``result`` with the
final answer, the token totals per model and the cost. Of the hook lines only a permission request and its answer are
relayed: Claude then waits for the user, which nothing else in the stream shows. Rate-limit lines are skipped. The ``init``
line also tells whether cmux attached its computer-use tools; a session without them gets a warning.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from typing import Any, ClassVar

from PIL import Image

from ....errors import RunnerCoreError
from ....processes import await_active
from ....usage import COUNTERS, empty_counts, estimate_cost
from ..standalone.events import this_turn
from .approvals import WAITING, pending_request
from .browser import is_browser_step, snapshots

DETAIL_LIMIT = 4000  # characters of one text block or tool result kept on an event
APPROVAL_HELP = ("Allow or deny it on this run's card, or in cmux: the Feed in the right sidebar (Ctrl-4) or the notification. "
                 "Claude waits about two minutes, then goes on as if it was denied.")
NO_COMPUTER_USE = ("cmux attaches them only when it finds its auth token, which macOS deletes from the temp folder once cmux has "
                   "run for three days. Restart cmux. This run goes on with the shell, files and the web only.")
TOKEN_FIELDS = {"input": "input_tokens", "cacheRead": "cache_read_input_tokens", "cacheWrite": "cache_creation_input_tokens",
                "output": "output_tokens"}  # our count → the field of an assistant message's usage block


class Surface:
    """What the runner files a screenshot under: where it came from."""

    mode = "headful"
    target_label = "cmux workspace (Claude Code)"
    viewport: ClassVar[dict[str, int]] = {"width": 1, "height": 1}  # every screenshot carries its own size

    async def read_state(self) -> dict[str, str]:
        return {"currentUrl": "cmux://workspace"}


def as_png(data: str) -> dict[str, Any]:
    """A tool result's image (base64, any format) → what the runner stores: PNG bytes and their size."""
    image = Image.open(io.BytesIO(base64.b64decode(data)))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return {"base64": base64.b64encode(buffer.getvalue()).decode(), "width": image.width, "height": image.height}


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


class StreamEvents:
    """Folds one run's stream into events on its context, and its usage onto the run record."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.surface = Surface()
        self.turn = 0
        self.seen: set[str] = set()  # assistant message ids already counted as a model call
        self.tools: dict[str, str] = {}  # tool call id → tool name, for the result that answers it
        self.commands: dict[str, str] = {}  # tool call id → the shell command, for a Bash call (a browser step gets screenshots)
        self.session_id = ""  # Claude's session, from its first line; cmux files a permission request under it
        self.calls: dict[str, tuple[str, str]] = {}  # tool name → its latest call: (call id, the text shown for it)
        self.asked: set[str] = set()  # call ids already reported as waiting for approval (each hook reports the request)
        self.models: dict[str, dict[str, Any]] = {}  # model → calls and tokens so far, counted from the messages
        self.result: dict[str, Any] | None = None  # the closing line; None while Claude is still working

    async def emit(self, kind: str, level: str, message: str, detail: str | None = None) -> None:
        event = {"type": kind, "level": level, "message": message, **({"detail": detail[:DETAIL_LIMIT]} if detail else {})}
        await await_active(self.context.emit_event(event), self.context.signal)

    async def handle(self, line: dict[str, Any]) -> None:
        kind = line.get("type")
        if kind == "system" and line.get("subtype") == "init":
            await self.started(line)
        elif kind == "system" and line.get("hook_event") == "PermissionRequest" and line.get("subtype") == "hook_started":
            await self.approval_requested(line["hook_name"].partition(":")[2])
        elif kind == "system" and line.get("hook_event") == "PermissionRequest" and line.get("subtype") == "hook_response":
            await self.approval_answered(line.get("output") or "")
        elif kind == "assistant":
            await self.assistant(line["message"])
        elif kind == "user":
            await self.tool_results(line["message"])
        elif kind == "result":
            self.finished(line)

    async def started(self, line: dict[str, Any]) -> None:
        self.context.detail["run"]["model"] = line["model"]
        self.session_id = line.get("session_id") or ""
        tools = line.get("tools") or []
        servers = ", ".join(f"{server['name']} {server['status']}" for server in line.get("mcp_servers") or [])
        await self.emit(
            "run_progress",
            "ok",
            "Claude Code session started.",
            f"{line['model']} · permission mode {line.get('permissionMode')} · {len(tools)} tools · {servers}",
        )
        if not any(tool.startswith("mcp__cmux-cua__") for tool in tools):
            await self.emit("run_progress", "warn", "Claude Code started without cmux's computer-use tools.", NO_COMPUTER_USE)

    async def approval_requested(self, tool: str) -> None:
        """Claude Code's settings ask the user before this call. A headless run cannot ask, so cmux does, in its Feed;
        the request found there is the one the console's Allow and Deny buttons answer."""
        call_id, call = self.calls.get(tool, (tool, tool))
        if call_id in self.asked:
            return
        self.asked.add(call_id)
        request = await await_active(pending_request(self.session_id, tool), self.context.signal)
        if request is not None:
            WAITING[self.context.detail["run"]["id"]] = request
        await self.emit("run_progress", "warn", f"Claude is waiting for your approval to use {tool}.", f"{call}\n{APPROVAL_HELP}")

    async def approval_answered(self, output: str) -> None:
        """The hook that carried the request to cmux's Feed returned: with a decision, or with none after two minutes.
        Other hooks on the same event print nothing."""
        if not output.strip().startswith("{"):
            return
        WAITING.pop(self.context.detail["run"]["id"], None)
        behavior = ((json.loads(output).get("hookSpecificOutput") or {}).get("decision") or {}).get("behavior")
        if behavior:
            await self.emit("run_progress", "ok", f"Approval answered: {behavior}.")
        else:
            await self.emit("run_progress", "warn", "Approval not answered in time.", "Claude goes on as if the call was denied.")

    async def assistant(self, message: dict[str, Any]) -> None:
        if message["id"] not in self.seen:  # one reply arrives as a line per content block, all with its id and usage
            self.seen.add(message["id"])
            self.turn += 1
            usage = message.get("usage") or {}
            counts = self.models.setdefault(message["model"], {"calls": 0, **{key: 0 for key in TOKEN_FIELDS}})
            counts["calls"] += 1
            for key, field in TOKEN_FIELDS.items():
                counts[key] += int(usage.get(field) or 0)
            self.context.detail["run"]["usage"] = usage_wire(self.models)
            sent = sum(int(usage.get(TOKEN_FIELDS[key]) or 0) for key in ("input", "cacheRead", "cacheWrite"))
            await self.emit(
                "run_progress",
                "ok",
                f"Claude turn {self.turn} completed.",
                f"{message['id']} · {sent} in · {int(usage.get('output_tokens') or 0)} out · 0 reasoning",
            )
        for block in message.get("content") or []:
            if block["type"] == "text":
                await self.emit("run_progress", "pending", "Model progress.", block["text"].strip())
            elif block["type"] == "thinking":
                await self.emit("run_progress", "pending", "Model reasoning.", (block.get("thinking") or "").strip())
            elif block["type"] == "tool_use":
                call = f"{block['name']} {json.dumps(block.get('input') or {})}"
                self.tools[block["id"]] = block["name"]
                if block["name"] == "Bash":
                    self.commands[block["id"]] = str((block.get("input") or {}).get("command") or "")
                self.calls[block["name"]] = (block["id"], call)
                await self.emit("function_call_requested", "pending", "Function tool call received from the model.", call)

    async def tool_results(self, message: dict[str, Any]) -> None:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") != "tool_result":
                continue
            name = self.tools.get(block["tool_use_id"], "tool")
            parts: Any = block.get("content") or []
            if isinstance(parts, str):  # a plain-text result
                parts = [{"type": "text", "text": parts}]
            for part in parts:
                if part["type"] == "text" and part["text"].strip():
                    await self.emit("tool_progress", "pending", f"{name} output.", part["text"].strip())
                elif part["type"] == "image":
                    shot = await asyncio.to_thread(as_png, part["source"]["data"])
                    await await_active(self.context.capture_screenshot(self.surface, name, shot), self.context.signal)
            await self.emit("function_call_completed", "warn" if block.get("is_error") else "ok", "Function tool call completed.", name)
            if name == "Bash" and is_browser_step(self.commands.get(block["tool_use_id"], "")) and not block.get("is_error"):
                await self.browser_snapshots()

    async def browser_snapshots(self) -> None:
        """What the agent's browser shows now, one run screenshot per page, after a step that touched it. They are for
        the run card: one cmux cannot take (a page that hung, say) is a warning, and the run goes on."""
        workspace = self.context.detail.get("actorCmuxBrowserWorkspace")
        if not workspace:
            return
        try:
            pages = await snapshots(workspace)
        except RunnerCoreError as error:
            await self.emit("run_progress", "warn", "Could not screenshot the agent browser.", str(error))
            return
        for page in pages:
            shot = await asyncio.to_thread(as_png, page["base64"])
            await await_active(self.context.capture_screenshot(self.surface, f"browser: {page['title'] or page['url']}"[:120], shot), self.context.signal)

    def finished(self, line: dict[str, Any]) -> None:
        """Claude's own totals replace the counts from the messages: they are exact, and they carry the cost. A resumed
        chat turn's totals are the whole Claude session's, so what the session had reported when the previous turn
        ended (``resumeTotals``) is taken off, and the new totals go on the chat block for the next turn."""
        self.result = line
        chat = self.context.detail.get("chat")
        totals = line.get("modelUsage") or {}
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
