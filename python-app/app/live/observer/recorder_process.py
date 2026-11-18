"""Starting and stopping the recorder subprocess (``python -m app.live.observer.recorder``) from the backend."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

from ...processes import child_environment
from ..config import LiveSettings
from ..storage.paths import SessionPaths


class ObserverProcess:
    def __init__(self, process: asyncio.subprocess.Process, log: Any) -> None:
        self.process = process
        self._log = log

    @classmethod
    async def spawn(cls, paths: SessionPaths, settings: LiveSettings, stream: Path, *, python_path: str | None = None) -> ObserverProcess:
        """``stream`` is data/memory/stream, the folder of daily parts the recorder appends its rows to as well."""
        arguments = [
            python_path or sys.executable, "-u", "-m", "app.live.observer.recorder",
            "--session-dir", str(paths.root),
            "--stream", str(stream),
            "--interval", str(settings.snapshot_interval_s),
            "--max-elements", str(settings.max_elements),
        ]
        arguments += ["--screenshots", settings.effective_screenshot_mode]
        log = paths.observer_log.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(*arguments, stdin=asyncio.subprocess.DEVNULL, stdout=log,
                                                           stderr=asyncio.subprocess.STDOUT, env=child_environment())
        except BaseException:
            log.close()
            raise
        return cls(process, log)

    @property
    def pid(self) -> int:
        return self.process.pid

    def alive(self) -> bool:
        return self.process.returncode is None

    async def stop(self, timeout: float = 8.0) -> int | None:
        if self.alive():
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self._log.close()
        return self.process.returncode
