"""The remote desktop as the person sees and drives it from the monitor.

The backend connects to the container's bridge as a viewer, beside the agent's own connection, twice: one worker for
the eye and one for the hand, so a click never waits behind a frame. ``frames`` runs a capture cell in the eye as fast
as it answers, up to ``FRAME_S`` apart: the cell screenshots, shrinks and JPEG-encodes in the container, so a few tens
of KB cross the tunnel per frame rather than a full-screen PNG. ``input`` runs the person's pointer and keyboard events
as PyAutoGUI calls in the hand; pointer moves that arrive while one runs collapse into the newest. The connections are
held while a stream is read and closed when the last viewer leaves, and with the session.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from typing import Any

from ....errors import RunnerCoreError
from ..desktop import remote as remote_desktops

FRAME_S = 0.1  # the least time between two captures; the eye's round trip is usually longer
KEYS = {"Enter": "enter", "Backspace": "backspace", "Tab": "tab", "Escape": "esc", "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
        "PageUp": "pageup", "PageDown": "pagedown", "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right", "Space": "space",
        "Control": "ctrl", "Alt": "alt", "Shift": "shift", "Meta": "ctrl"}  # the viewer's key names (a browser's) → PyAutoGUI's; ⌘ acts as Ctrl there
# The capture cell: the screen as a JPEG no wider than the viewer needs, logged as "<width> <height> <base64>" (the
# screen's size first, for the pointer's mapping). A cell may log 60 KB of text in all, so the JPEG is encoded at
# falling quality until it fits, and shrunk if it still does not. mss grabs the screen far faster than PyAutoGUI's
# screenshot when the container has it.
BUDGET = 56_000  # base64 characters per frame, under the worker's limit with the header to spare
CAPTURE = """import base64, io
from PIL import Image
try:
    import mss
    with mss.mss() as grab:
        raw = grab.grab(grab.monitors[0])
        image = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
except Exception:  # not installed, or unable to grab this display
    image = pyautogui.screenshot()
size = image.size
image = image.convert('RGB')
image.thumbnail(({width}, {width}))
while True:
    for quality in (60, 45, 30, 20):
        buffer = io.BytesIO()
        image.save(buffer, 'JPEG', quality=quality)
        data = base64.b64encode(buffer.getvalue()).decode()
        if len(data) <= {budget}:
            break
    else:
        image.thumbnail((image.width // 2, image.height // 2))
        continue
    break
log('%d %d ' % size + data)
"""


def code_for(event: dict[str, Any], size: tuple[int, int]) -> str:
    """The PyAutoGUI call for one viewer event; ``x`` and ``y`` are fractions of the frame."""
    kind = event.get("kind")
    x, y = round(float(event.get("x", 0)) * size[0]), round(float(event.get("y", 0)) * size[1])
    button = event.get("button") if event.get("button") in ("left", "right", "middle") else "left"
    if kind == "move":
        return f"pyautogui.moveTo({x}, {y})"
    if kind == "down":
        return f"pyautogui.mouseDown({x}, {y}, button={button!r})"
    if kind == "up":
        return f"pyautogui.mouseUp({x}, {y}, button={button!r})"
    if kind == "click":  # a right or middle button: pressed and released at once
        return f"pyautogui.click({x}, {y}, button={button!r})"
    if kind == "wheel":
        return f"pyautogui.scroll({-round(float(event.get('dy', 0)) / 40)}, {x}, {y})"
    if kind == "text":
        return f"pyautogui.write({str(event['text'])!r})"
    if kind == "key":
        parts = [KEYS.get(part, part.lower() if len(part) > 1 else part) for part in str(event["key"]).split("+")]
        return f"pyautogui.hotkey({', '.join(map(repr, parts))})" if len(parts) > 1 else f"pyautogui.press({parts[0]!r})"
    raise RunnerCoreError(f"Unknown input kind {kind!r}.", code="invalid_request", status_code=400)


async def run(runtime: Any, code: str) -> list[dict[str, Any]]:
    """One cell in a viewer's worker: its output items."""
    cell = await runtime.start(code)
    while True:
        message = await cell.next(10.0)
        if "output" in message:
            return message["output"]


class RemoteView:
    def __init__(self, provider: str, url: str, token: str) -> None:
        self.target = (provider, url, token)
        self.eye: Any = None   # the worker that captures
        self.hand: Any = None  # the worker that acts
        self.viewers = 0
        self.eye_lock = asyncio.Lock()
        self.hand_lock = asyncio.Lock()
        self.latest_move: dict[str, Any] | None = None  # a pointer move that arrived while the hand was busy
        self.size: tuple[int, int] | None = None  # the screen's, from the last capture

    async def connect(self) -> None:
        if self.eye is None:
            self.eye = await remote_desktops.connect(*self.target, role="viewer")
            self.hand = await remote_desktops.connect(*self.target, role="viewer")

    async def release(self) -> None:
        """Close both connections; any stream ends on its next capture."""
        eye, hand, self.eye, self.hand = self.eye, self.hand, None, None
        for runtime in (eye, hand):
            if runtime is not None:
                await runtime.close()

    async def frames(self, max_width: int) -> AsyncIterator[bytes]:
        self.viewers += 1
        try:
            await self.connect()
            code = CAPTURE.format(width=max(160, min(max_width, 1920)), budget=BUDGET)
            while self.eye is not None:
                started = time.monotonic()
                async with self.eye_lock:
                    items = await run(self.eye, code)
                logged = next((item["text"] for item in items if item["type"] == "input_text" and item["text"][:1].isdigit()), None)
                if not logged:  # the cell failed: its traceback, so the log says why rather than the monitor staying blank
                    raise RuntimeError("The remote desktop's capture failed: " + " | ".join(item.get("text", "")[-300:] for item in items if item["type"] == "input_text"))
                width, height, data = logged.split(" ", 2)
                self.size = (int(width), int(height))
                yield base64.b64decode(data)
                await asyncio.sleep(max(0.0, FRAME_S - (time.monotonic() - started)))
        finally:
            self.viewers -= 1
            if self.viewers == 0:
                await self.release()

    async def input(self, event: dict[str, Any]) -> None:
        if self.hand is None or self.size is None:
            raise RunnerCoreError("The remote desktop is not being watched.", code="remote_view_closed", status_code=409,
                                  hint="Open it on the monitor first.")
        if event.get("kind") == "move" and self.hand_lock.locked():
            self.latest_move = event  # the newest move is the one that matters
            return
        async with self.hand_lock:
            await run(self.hand, f"import pyautogui\npyautogui.PAUSE = 0\n{code_for(event, self.size)}")
            while (move := self.latest_move) is not None:
                self.latest_move = None
                await run(self.hand, f"import pyautogui\npyautogui.PAUSE = 0\n{code_for(move, self.size)}")
