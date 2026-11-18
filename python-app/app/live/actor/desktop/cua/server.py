"""The remote desktop's worker bridge: runs inside the cua container, one WebSocket in, the same worker out.

    python3 server.py --port 8100 --token <secret> [--worker /opt/actor/worker.py]

Each connection spawns a fresh worker, the same worker.py the Mac runs, in pyautogui mode on the container's X
display, and relays its JSON lines both ways: requests from the socket to the worker's stdin, its progress and
results back over the socket. One extra message, {"operation": "interrupt"}, becomes SIGINT to the worker, the
interrupt the local runtime sends. The first message must be {"token": <secret>}; anything else closes the socket.
One agent connection at a time, since there is one desktop; a connection whose first message adds "role": "viewer"
(the person watching from the monitor, and stepping in) is let in beside it, any number of them, each with a worker
of its own on the same display. Closing the socket kills the worker, so every run starts from a clean REPL.
GET /status answers 200 for health checks.

Nothing here imports the rest of the app: the file runs alone in the container.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http
import json
import signal
import sys
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

MAX_MESSAGE = 12 * 1024 * 1024  # a worker line can carry a screenshot; the local runtime allows the same


class Bridge:
    def __init__(self, token: str, worker: Path) -> None:
        self.token = token
        self.worker = worker
        self.busy = False

    async def handle(self, socket: ServerConnection) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(socket.recv(), 10))
        except (asyncio.TimeoutError, ValueError, ConnectionClosed):
            await socket.close(1008, "token expected")
            return
        if not isinstance(hello, dict) or hello.get("token") != self.token:
            await socket.close(1008, "bad token")
            return
        viewer = hello.get("role") == "viewer"
        if self.busy and not viewer:
            await socket.close(1013, "busy")
            return
        self.busy = self.busy or not viewer
        try:
            await self.relay(socket)
        finally:
            if not viewer:
                self.busy = False

    async def relay(self, socket: ServerConnection) -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(self.worker), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr, limit=MAX_MESSAGE)
        assert worker.stdin is not None and worker.stdout is not None
        stdin, stdout = worker.stdin, worker.stdout

        async def to_socket() -> None:  # the worker's lines, ready message included, verbatim
            while line := await stdout.readline():
                await socket.send(line.decode("utf-8").rstrip("\n"))
            await socket.close(1011, "worker exited")

        async def to_worker() -> None:
            async for message in socket:
                request: dict[str, Any] = json.loads(message)
                if request.get("operation") == "interrupt":
                    worker.send_signal(signal.SIGINT)
                    continue
                stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                await stdin.drain()

        pumps = [asyncio.create_task(to_socket()), asyncio.create_task(to_worker())]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            stdin.close()  # a process the cell started may hold the pipe's other end; wait() needs ours closed
            with contextlib.suppress(ProcessLookupError):
                worker.kill()
            await worker.wait()


def status(connection: ServerConnection, request: Any) -> Any:
    """GET /status → 200 for a health check; every other path is the WebSocket."""
    if request.path == "/status":
        return connection.respond(http.HTTPStatus.OK, "ready\n")
    return None


async def serve_bridge(host: str, port: int, token: str, worker: Path) -> Any:
    """The listening server; ``async with`` it, or await ``serve_forever()``."""
    return await serve(Bridge(token, worker).handle, host, port, process_request=status, max_size=MAX_MESSAGE)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge one WebSocket to the live actor's desktop worker.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--token", required=True, help="the shared secret the client must present first")
    parser.add_argument("--worker", type=Path, default=Path(__file__).resolve().parent.parent / "worker.py")
    args = parser.parse_args()
    async with await serve_bridge(args.host, args.port, args.token, args.worker) as server:
        print(f"worker bridge on {args.host}:{args.port}, worker {args.worker}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
