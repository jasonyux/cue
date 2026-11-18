"""The remote desktop from the Mac's side: the actor's second runtime, over one WebSocket to the container's bridge.

``RemoteRuntime`` has the cell interface of ``desktop/runtime.py``'s ``PythonRuntime``, so ``exec_py``, yielding,
``wait_cell`` and ``stop_cell`` work the same on either target; only the transport differs. The worker at the far end
is the same worker.py, so the protocol is identical: one JSON message per line of it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .....processes import assert_active, await_active
from ..runtime import Cell, PythonRuntimeError

MAX_MESSAGE = 12 * 1024 * 1024


class RemoteRuntime:
    """One connection to the bridge: a fresh worker on the remote desktop for this run."""

    def __init__(self, socket: ClientConnection, url: str, signal: asyncio.Event | None) -> None:
        self.socket = socket
        self.url = url
        self.signal = signal
        self.platform = "linux"
        self.mode = "pyautogui"
        self.label = "an isolated Ubuntu desktop (remote) with its own browser and none of the user's files, apps or accounts"
        self._sequence = 0
        self._busy = False
        self.cell: Cell | None = None
        self._closed = False

    async def _read_message(self, expected_id: int | None, timeout_ms: int, signal: asyncio.Event | None = None) -> dict[str, Any]:
        """One worker line from the socket, with the local runtime's checks; asyncio.TimeoutError when nothing arrives."""
        try:
            raw = await asyncio.wait_for(await_active(self.socket.recv(), signal or self.signal), timeout_ms / 1000)
        except ConnectionClosed as error:
            reason = f": {error.rcvd.reason}" if error.rcvd is not None and error.rcvd.reason else ""
            raise PythonRuntimeError(f"Remote desktop connection closed{reason}.") from error
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("Invalid response.")
            if expected_id is not None and value.get("id") != expected_id:
                raise ValueError("Unexpected response ID.")
            if expected_id is None and "id" in value:
                raise ValueError("Unexpected startup response ID.")
        except (TypeError, ValueError, UnicodeError) as error:
            raise PythonRuntimeError("Remote worker returned invalid JSON or response ID.") from error
        if "error" in value:
            raise PythonRuntimeError(value["error"])
        return value

    async def _send(self, request: dict[str, Any], signal: asyncio.Event | None) -> None:
        try:
            await await_active(self.socket.send(json.dumps(request)), signal or self.signal)
        except ConnectionClosed as error:
            raise PythonRuntimeError("Remote desktop connection closed.") from error

    async def start(self, code: str, signal: asyncio.Event | None = None) -> Cell:
        """Begin a cell on the remote desktop and return at once; its output arrives through ``Cell.next``."""
        if self._closed:
            raise PythonRuntimeError("Remote desktop session closed.")
        if self._busy:
            raise PythonRuntimeError("A remote operation is already running.")
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 64 * 1024:
            raise PythonRuntimeError("Python code must be nonempty and at most 64 KiB.")
        self._busy = True
        try:
            assert_active(self.signal)
            assert_active(signal)
            self._sequence += 1
            await self._send({"id": self._sequence, "operation": "execute", "code": code}, signal)
        except BaseException:
            self._busy = False
            await self.close()
            raise
        self.cell = Cell(self, self._sequence, signal, prefix="r")
        return self.cell

    def interrupt(self) -> None:
        """SIGINT to the remote worker, through the bridge."""
        asyncio.get_running_loop().create_task(self._send({"operation": "interrupt"}, None))

    async def capture(self, signal: asyncio.Event | None = None) -> dict[str, Any]:
        """A screenshot of the remote desktop: {"image": base64 PNG or None, "app": str, "title": str}."""
        if self._closed:
            raise PythonRuntimeError("Remote desktop session closed.")
        if self._busy:
            raise PythonRuntimeError("A remote operation is already running.")
        self._busy = True
        try:
            self._sequence += 1
            await self._send({"id": self._sequence, "operation": "capture"}, signal)
            while True:
                try:
                    value = await self._read_message(self._sequence, 30_000, signal)
                except asyncio.TimeoutError as error:
                    raise PythonRuntimeError("Remote screenshot took longer than 30s.") from error
                if "progress" not in value:
                    break
            capture = value.get("capture")
            if not isinstance(capture, dict):
                raise PythonRuntimeError("Remote worker returned no capture.")
            return capture
        except BaseException:
            await self.close()
            raise
        finally:
            self._busy = False

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.socket.close()


async def connect_remote(url: str, token: str, *, signal: asyncio.Event | None = None, timeout_s: float = 20.0,
                         role: str | None = None) -> RemoteRuntime:
    """Open the bridge, present the token (and the role: a "viewer" is let in beside the agent), and wait for the
    worker's ready line."""
    assert_active(signal)
    try:
        socket = await connect(url, max_size=MAX_MESSAGE, open_timeout=timeout_s)
    except Exception as error:
        raise PythonRuntimeError(f"Could not reach the remote desktop at {url}: {type(error).__name__}: {error}"[:500]) from error
    runtime = RemoteRuntime(socket, url, signal)
    try:
        await runtime._send({"token": token, **({"role": role} if role else {})}, signal)
        ready = await runtime._read_message(None, int(timeout_s * 1000))
    except asyncio.TimeoutError as error:
        await runtime.close()
        raise PythonRuntimeError("The remote desktop's worker did not report ready in time.") from error
    except BaseException:
        await runtime.close()
        raise
    if ready.get("ready") is not True:
        await runtime.close()
        raise PythonRuntimeError("The remote desktop's worker did not initialize.")
    runtime.platform = str(ready.get("platform") or runtime.platform)
    runtime.mode = str(ready.get("mode") or runtime.mode)
    return runtime


def png_observation(encoded: str) -> dict[str, Any]:
    """A base64 PNG as the runner's screenshot observation: the data plus its pixel size from the header."""
    image = base64.b64decode(encoded)
    observation: dict[str, Any] = {"base64": encoded}
    if len(image) >= 24 and image[:8] == b"\x89PNG\r\n\x1a\n":
        observation["width"], observation["height"] = struct.unpack(">II", image[16:24])
    return observation
