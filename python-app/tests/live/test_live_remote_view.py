"""The remote desktop on the monitor between runs: screenshots streamed, the person's input run through PyAutoGUI."""

import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from live_helpers import make_manager
from PIL import Image

from app.api import create_app
from app.errors import RunnerCoreError
from app.live.actor.standalone import remote_view
from app.runner import RunnerManager


def jpeg(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeCell:
    def __init__(self, output):
        self.output = output
        self.done = False

    async def next(self, timeout_s):
        await asyncio.sleep(0.01)  # a round trip to the container
        self.done = True
        return {"output": self.output}


class FakeRemote:
    """A viewer's worker: a capture cell logs the screen's size and a 640-wide JPEG; any other cell answers nothing."""

    def __init__(self):
        self.codes: list[str] = []
        self.closed = False
        self.cell = None

    async def start(self, code, signal=None):
        self.codes.append(code)
        output = [{"type": "input_text", "text": f"1920 1080 {jpeg(640, 360)}"}] if "thumbnail" in code else []
        self.cell = FakeCell(output)
        return self.cell

    async def close(self):
        self.closed = True


@pytest.fixture
def remotes(monkeypatch):
    """The two viewer connections the view opens: the eye, then the hand."""
    fakes = [FakeRemote(), FakeRemote()]
    opened = iter(fakes)

    async def connect(provider, url, token, **kwargs):
        assert (provider, url, token, kwargs) == ("cua", "wss://desk.example", "secret-token", {"role": "viewer"})  # beside the agent's connection
        return next(opened)

    monkeypatch.setattr(remote_view.remote_desktops, "connect", connect)
    monkeypatch.setattr(remote_view, "FRAME_S", 0)
    return fakes


async def test_the_view_streams_captures_runs_input_and_lets_go_when_the_viewer_leaves(remotes):
    eye, hand = remotes
    view = remote_view.RemoteView("cua", "wss://desk.example", "secret-token")
    with pytest.raises(RunnerCoreError) as closed:
        await view.input({"kind": "key", "key": "Enter"})
    assert closed.value.code == "remote_view_closed"
    frames = view.frames(640)
    frame = await anext(frames)
    assert frame[:2] == b"\xff\xd8" and Image.open(io.BytesIO(frame)).size == (640, 360) and view.size == (1920, 1080)
    assert "image.thumbnail((640, 640))" in eye.codes[0] and hand.codes == []  # the eye captures, shrunk in the container
    await view.input({"kind": "down", "x": 0.5, "y": 0.5, "button": "left"})
    await view.input({"kind": "up", "x": 0.5, "y": 0.5, "button": "left"})
    await view.input({"kind": "text", "text": "hi"})
    await view.input({"kind": "key", "key": "Meta+a"})
    await view.input({"kind": "key", "key": "Enter"})
    await view.input({"kind": "wheel", "x": 0.25, "y": 0.5, "dy": 120})
    await view.input({"kind": "click", "x": 0.5, "y": 0.5, "button": "right"})
    prefix = "import pyautogui\npyautogui.PAUSE = 0\n"
    assert hand.codes == [f"{prefix}pyautogui.mouseDown(960, 540, button='left')", f"{prefix}pyautogui.mouseUp(960, 540, button='left')",
                          f"{prefix}pyautogui.write('hi')", f"{prefix}pyautogui.hotkey('ctrl', 'a')", f"{prefix}pyautogui.press('enter')",
                          f"{prefix}pyautogui.scroll(-3, 480, 540)", f"{prefix}pyautogui.click(960, 540, button='right')"]
    assert all("thumbnail" in code for code in eye.codes)  # the hand never captures, the eye never acts
    with pytest.raises(RunnerCoreError, match="Unknown input kind"):
        await view.input({"kind": "dance"})
    await frames.aclose()
    assert eye.closed and hand.closed and view.eye is None  # the last viewer gone: both connections closed


async def test_moves_that_arrive_while_the_hand_is_busy_collapse_into_the_newest(remotes):
    _eye, hand = remotes
    view = remote_view.RemoteView("cua", "wss://desk.example", "secret-token")
    frames = view.frames(640)
    await anext(frames)
    first = asyncio.create_task(view.input({"kind": "down", "x": 0.1, "y": 0.1}))
    await asyncio.sleep(0)  # the hand is on the click
    await view.input({"kind": "move", "x": 0.2, "y": 0.2})
    await view.input({"kind": "move", "x": 0.3, "y": 0.3})
    await first
    assert [code.splitlines()[-1] for code in hand.codes] == ["pyautogui.mouseDown(192, 108, button='left')", "pyautogui.moveTo(576, 324)"]
    await frames.aclose()


async def test_a_failing_capture_says_why(remotes, monkeypatch):
    eye, _hand = remotes

    async def failing(code, signal=None):
        return FakeCell([{"type": "input_text", "text": "Traceback …\nScreenShotError: no display"}])

    monkeypatch.setattr(eye, "start", failing)
    view = remote_view.RemoteView("cua", "wss://desk.example", "secret-token")
    with pytest.raises(RuntimeError, match="capture failed: Traceback"):
        await anext(view.frames(640))


async def test_releasing_the_view_ends_its_stream(remotes):
    view = remote_view.RemoteView("cua", "wss://desk.example", "secret-token")
    frames = view.frames(640)
    await anext(frames)
    await view.release()  # the session stopping
    with pytest.raises(StopAsyncIteration):
        await anext(frames)
    assert all(fake.closed for fake in remotes)


class FakeView:
    def __init__(self):
        self.inputs: list[dict] = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def frames(self, max_width):
        yield b"\xff\xd8one"

    async def input(self, event):
        self.inputs.append(event)


async def test_the_monitor_reads_and_drives_the_remote_desktop_through_the_routes(tmp_path, monkeypatch):
    live, _client, _ = make_manager(tmp_path / "data")
    fake = FakeView()
    monkeypatch.setattr(live, "remote_view", lambda: fake)
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        streamed = await http.get("/api/live/session/remote/stream", params={"width": 640})
        assert fake.connected and streamed.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert streamed.content == b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n\xff\xd8one\r\n--frame\r\n"
        assert (await http.post("/api/live/session/remote/input", json={"kind": "key", "key": "Enter"})).json() == {"ok": True}
        assert fake.inputs == [{"kind": "key", "key": "Enter"}]


async def test_without_a_remote_desktop_the_monitor_is_told(tmp_path, monkeypatch):
    live, _client, _ = make_manager(tmp_path / "data")
    monkeypatch.setattr(live, "_active", lambda: SimpleNamespace(remote_view=None))
    monkeypatch.setattr(live, "desktop_remote", lambda: None)
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/session/remote/stream")).json()["code"] == "remote_unavailable"
