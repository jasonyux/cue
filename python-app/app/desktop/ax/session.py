"""A run session for desktop scenarios: no lab server, no browser, screenshots come from the worker."""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from ..placeholder import placeholder_png
from ..runtime import PythonRuntime


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DesktopSession:
    """Mirrors the BrowserSession surface the runner relies on for timeline screenshots and state."""

    def __init__(self, *, runtime: PythonRuntime, screenshot_dir: Path, target_label: str,
                 signal: asyncio.Event | None = None) -> None:
        self.execution = runtime
        self.screenshot_dir = screenshot_dir
        self.target_label = target_label
        self.signal = signal
        self.mode = "headful"
        self.viewport: dict[str, int] = {"width": 640, "height": 120}
        self.current_app = ""
        self.window_title = ""
        self._count = 0

    async def read_state(self) -> dict[str, Any]:
        state = {"currentUrl": f"desktop://{self.current_app or 'local'}"}
        if self.window_title:
            state["pageTitle"] = self.window_title
        return state

    async def frontmost(self) -> None:
        return None

    async def capture_screenshot(self, label: str) -> dict[str, Any]:
        capture = await self.execution.capture()
        placeholder = not capture.get("image")
        png = placeholder_png() if placeholder else base64.b64decode(capture["image"])
        self.current_app = str(capture.get("app") or self.current_app)
        self.window_title = str(capture.get("title") or "")
        with Image.open(io.BytesIO(png)) as image:
            self.viewport = {"width": max(1, image.width), "height": max(1, image.height)}
        self._count += 1
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        name = "".join(character if character.isalnum() or character in "-_" else "-" for character in label)[:60]
        path = self.screenshot_dir / f"{self._count:03d}-{name or 'desktop'}.png"
        await asyncio.to_thread(path.write_bytes, png)
        record = {**await self.read_state(), "id": f"screenshot-{self._count}", "label": label, "path": str(path),
                  "mimeType": "image/png", "capturedAt": timestamp()}
        if placeholder:
            record["placeholder"] = True  # the console's review strip skips these; the timeline keeps them
        return record

    async def close(self) -> None:
        return None
