"""The remote desktop: the bridge that runs in the container and the client on the Mac, over a loopback socket."""

import asyncio
import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from app.live.actor.desktop.cua.client import connect_remote, png_observation
from app.live.actor.desktop.cua.server import serve_bridge
from app.live.actor.desktop.runtime import PythonRuntimeError

FIXTURES = Path(__file__).parent / "fixtures"


async def drain(cell):
    """Every event of a cell until its output: the progress items, then the final item list."""
    progress, output = [], None
    while output is None:
        message = await cell.next(5.0)
        if "progress" in message:
            progress.append(message["progress"])
        elif "output" in message:
            output = message["output"]
    return progress, output


async def test_bridge_relays_cells_interrupts_and_captures(unused_tcp_port):
    url = f"ws://127.0.0.1:{unused_tcp_port}"
    async with await serve_bridge("127.0.0.1", unused_tcp_port, "secret", FIXTURES / "actor_worker.py"):
        with pytest.raises(PythonRuntimeError, match="bad token"):
            await connect_remote(url, "wrong")
        runtime = await connect_remote(url, "secret")
        assert runtime.mode == "fake" and runtime.platform == sys.platform  # the worker's ready line, relayed
        cell = await runtime.start("log('a')\nlog('b')")
        assert cell.id == "r1" and runtime.cell is cell
        progress, output = await drain(cell)
        assert [item["text"] for item in progress] == ["a", "b"] and [item["text"] for item in output] == ["a", "b"]
        assert cell.done and runtime.cell is None
        with pytest.raises(PythonRuntimeError, match="busy"):  # one desktop, one connection at a time
            await connect_remote(url, "secret")
        cell = await runtime.start("log('start')\nwhile True:\n    time.sleep(0.05)")
        assert (await cell.next(5.0)) == {"progress": {"type": "input_text", "text": "start"}}
        cell.interrupt()  # SIGINT through the bridge: the partial output plus the traceback come back
        _progress, output = await drain(cell)
        assert output[0]["text"] == "start" and "KeyboardInterrupt" in output[-1]["text"]
        assert (await runtime.capture())["image"] is None  # the bare fixture has no desktop to capture
        cell = await runtime.start("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\nlog('spawned')")
        assert (await drain(cell))[1][0]["text"] == "spawned"
        await runtime.close()
        await asyncio.sleep(0.3)  # the bridge reaps the worker, even with the process the cell started still alive
        again = await connect_remote(url, "secret")
        cell = await again.start("log('fresh')")
        assert (await drain(cell))[1][0]["text"] == "fresh"
        await again.close()


async def test_a_viewer_is_let_in_beside_the_agent(unused_tcp_port):
    """The monitor's view of the remote desktop: a connection with role viewer, allowed while the agent has the bridge,
    with a worker of its own; it does not take the agent's slot."""
    async with await serve_bridge("127.0.0.1", unused_tcp_port, "secret", FIXTURES / "actor_worker.py"):
        url = f"ws://127.0.0.1:{unused_tcp_port}"
        agent = await connect_remote(url, "secret")
        viewer = await connect_remote(url, "secret", role="viewer")
        with pytest.raises(PythonRuntimeError, match="busy"):  # a second agent is still refused
            await connect_remote(url, "secret")
        _, output = await drain(await viewer.start("log('seen by the viewer')"))
        assert output == [{"type": "input_text", "text": "seen by the viewer"}]
        _, output = await drain(await agent.start("log('the agent too')"))
        assert output == [{"type": "input_text", "text": "the agent too"}]
        await viewer.close()
        await agent.close()
        another = await connect_remote(url, "secret", role="viewer")  # a viewer alone leaves the agent's slot free
        agent = await connect_remote(url, "secret")
        await another.close()
        await agent.close()


async def test_bridge_status_endpoint_answers_health_checks(unused_tcp_port):
    async with await serve_bridge("127.0.0.1", unused_tcp_port, "secret", FIXTURES / "actor_worker.py"):
        reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
        writer.write(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        head = await reader.read(64)
        writer.close()
        assert head.startswith(b"HTTP/1.1 200")


def test_png_observation_reads_the_image_size():
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    assert png_observation(encoded) == {"base64": encoded, "width": 40, "height": 30}
    assert png_observation(base64.b64encode(b"not a png").decode("ascii")) == {"base64": base64.b64encode(b"not a png").decode("ascii")}
