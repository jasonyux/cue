"""The desktop MCP server: the desktop agent's worker on the user's Mac, for an agent that takes its tools over MCP.

A standalone agent starts it over stdio (``servers.py``) and gets exec_py, wait_cell and stop_cell with the desktop
agent's own descriptions and checks (``desktop/tools.py``). The desktops are the session's (``CUA_ACTOR_DESKTOP_SOURCE``):
the user's Mac, where the worker runs in accessibility mode whatever ``CUA_DESKTOP_MODE`` says, acting through the
Accessibility API so it never moves the user's mouse or takes their keyboard focus; the remote desktop
(``CUA_DESKTOP_REMOTE_*``, the token from the backend's environment, which this server inherits); or both, and then
exec_py takes a ``target``. Each starts on its first call. What a cell logs and displays comes back as the call's
result; the agent's stream carries it to the run, which files the images as screenshots. After a step on the Mac, the
window the agent works in, as it is then, is left as a PNG in ``CUA_DESKTOP_FRAMES`` for the run's record (the relay
files it, ``events.py``); the agent itself reads the tree. Of MCP it answers what an agent's tool calls need:
initialize, ping, tools/list and tools/call.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from ...config import ACTOR_YIELD_S_DEFAULT
from ..desktop import remote as remote_desktops
from ..desktop import runtime
from ..desktop.tools import (
    MAX_WAIT_S,
    REMOTE_DESKTOP,
    STOP_CELL,
    WAIT_CELL,
    exec_py_definition,
    validate_cell_args,
    validate_exec_py,
)

LINE_LIMIT = 64 * 1024 * 1024  # one request line; generous, like the agent's own


def desktops() -> tuple[str, ...]:
    """The agent's desktops: local, remote, or both (then exec_py takes a target), as the run's server config says."""
    return tuple(part.strip() for part in os.environ.get("CUA_ACTOR_DESKTOP_SOURCE", "local").split(",") if part.strip())


def tools() -> list[dict[str, Any]]:
    source = desktops()
    if source == ("remote",):
        exec_py = exec_py_definition("the remote desktop, an isolated Ubuntu desktop", "pyautogui", ACTOR_YIELD_S_DEFAULT)
    else:
        exec_py = exec_py_definition("the user's own Mac", "accessibility", ACTOR_YIELD_S_DEFAULT, REMOTE_DESKTOP if "remote" in source else None)
    return [exec_py, WAIT_CELL, STOP_CELL]


def text(value: str) -> dict[str, Any]:
    return {"type": "input_text", "text": value}


def listed(definition: dict[str, Any]) -> dict[str, Any]:
    """A tool as the desktop agent defines it (a Responses API function) → as MCP lists it."""
    return {"name": definition["name"], "description": definition["description"], "inputSchema": definition["parameters"]}


IMAGE_MAX_SIDE = 1568  # the long edge Claude's API scales an image down to anyway
IMAGE_QUALITY = 80


def shrunk(data_url: str) -> str:
    """A PNG data URL → base64 JPEG no larger than IMAGE_MAX_SIDE. A desktop as PNG runs to megabytes, and the
    Claude Agent SDK reads a tool result as one message with a size limit."""
    image = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=IMAGE_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def content(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The worker's output items → MCP content: text stays text, a PNG data URL becomes a (smaller) JPEG image."""
    return [
        {"type": "image", "mimeType": "image/jpeg", "data": shrunk(item["image_url"])}
        if item["type"] == "input_image" else {"type": "text", "text": item["text"]}
        for item in items
    ]


async def follow(cell: Any, budget_s: float) -> list[dict[str, Any]]:
    """A cell's output for up to ``budget_s``: what it logged and displayed, then its completion, or a note that it
    still runs."""
    deadline = time.monotonic() + budget_s
    items: list[dict[str, Any]] = []
    while (remaining := deadline - time.monotonic()) > 0:
        message = await cell.next(min(remaining, 5.0))
        if "progress" in message:
            items.append(message["progress"])
        elif "output" in message:
            return items + message["output"][cell.delivered:] or [text("exec_py completed with no output.")]
    return [*items, text(f"Still running as cell {cell.id}. Call wait_cell to get more output or its completion, or stop_cell to interrupt it.")]


class Desktop:
    """The desktops' workers, each started on its first call, and the one cell that runs across them at a time."""

    def __init__(self) -> None:
        self.source = desktops()
        self.runtimes: dict[str, Any] = {}  # target → its worker
        self.target = ""  # the desktop of the latest exec_py
        self.frames = Path(os.environ["CUA_DESKTOP_FRAMES"]) if os.environ.get("CUA_DESKTOP_FRAMES") else None

    async def runtime(self, target: str) -> Any:
        if target not in self.runtimes:
            if target == "remote":
                self.runtimes[target] = await remote_desktops.connect(os.environ.get("CUA_DESKTOP_REMOTE_PROVIDER", ""), os.environ.get("CUA_DESKTOP_REMOTE_URL", ""),
                                                                      os.environ.get("CUA_DESKTOP_REMOTE_TOKEN", ""))
            else:
                self.runtimes[target] = await runtime.launch_python_runtime()
        return self.runtimes[target]

    def running(self) -> Any:
        """The cell in progress on either desktop, if any."""
        return next((worker.cell for worker in self.runtimes.values() if worker.cell is not None and not worker.cell.done), None)

    async def call(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        both = len(self.source) == 2
        checks = {"exec_py": lambda a: validate_exec_py(a, both), "wait_cell": lambda a: validate_cell_args(a, with_timeout=True),
                  "stop_cell": lambda a: validate_cell_args(a, with_timeout=False)}
        if name not in checks:
            raise ValueError(f"Unknown tool {name}.")
        if problem := checks[name](args):
            raise ValueError(problem)
        cell = self.running()
        if name == "exec_py":
            if cell is not None:
                return [text(f"Cell {cell.id} is still running; one cell runs at a time. Call wait_cell or stop_cell first.")]
            self.target = target = args["target"] if both else self.source[0]
            worker = await self.runtime(target)
            cell = await worker.start(args["code"], None)
            items = await follow(cell, ACTOR_YIELD_S_DEFAULT)
            if target == "remote":  # PyAutoGUI: the agent sees the screen after its step, as the desktop agent's run does; the run files it too
                shot = await worker.capture(None)
                if shot.get("image"):
                    items.append({"type": "input_image", "image_url": f"data:image/png;base64,{shot['image']}"})
            await self.frame(cell)
            return items
        if cell is None or cell.id != args["cell"]:
            return [text(f"No running cell {args['cell']}.")]
        if name == "wait_cell":
            items = await follow(cell, min(float(args["timeout_s"]), MAX_WAIT_S))
        else:
            cell.interrupt()
            items = await follow(cell, 15.0)
        await self.frame(cell)
        return items

    async def frame(self, cell: Any) -> None:
        """A step on the Mac done: the window the agent works in, as it is now, left for the run's record. Nothing while the
        cell still runs, or before the agent looked at any window."""
        if self.frames is None or self.target != "local" or not cell.done:
            return
        shot = await self.runtimes["local"].capture(None)
        if shot.get("image"):
            self.frames.mkdir(parents=True, exist_ok=True)
            (self.frames / f"{time.time_ns()}.png").write_bytes(base64.b64decode(shot["image"]))

    async def close(self) -> None:
        for worker in self.runtimes.values():
            await worker.close()


async def answer(desktop: Desktop, request: dict[str, Any]) -> dict[str, Any]:
    """The JSON-RPC reply to one request, without its id: a result, or an error for a method this server lacks."""
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        return {"result": {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                           "serverInfo": {"name": "computerpilot-desktop", "version": "1"}}}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": [listed(tool) for tool in tools()]}}
    if method == "tools/call":
        try:
            return {"result": {"content": content(await desktop.call(params["name"], params.get("arguments") or {}))}}
        except Exception as error:  # noqa: BLE001 - every failure goes back to the agent as the call's result
            return {"result": {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}], "isError": True}}
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


async def serve() -> None:
    """Answer requests from stdin, one at a time, until the agent closes it; then close the worker."""
    desktop = Desktop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while line := await reader.readline():
            request = json.loads(line)
            if "id" in request:  # a notification wants no answer
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], **await answer(desktop, request)}) + "\n")
                sys.stdout.flush()
    finally:
        await desktop.close()


if __name__ == "__main__":
    os.environ["CUA_DESKTOP_MODE"] = "accessibility"  # the worker reads it: never the user's mouse and keyboard
    asyncio.run(serve())
