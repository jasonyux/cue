"""The agent browser: a headless Chromium the agent drives and the person watches and steers from the monitor."""

import asyncio
import base64
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from live_helpers import make_manager

from app.api import create_app
from app.errors import RunnerCoreError
from app.live.actor.standalone import browser as agent_browser
from app.runner import RunnerManager

FORM = "data:text/html,<input id=a style='position:absolute;left:100px;top:100px;width:300px;height:40px'><p id=o></p><script>a.oninput=()=>o.textContent=a.value</script>"


@pytest.fixture
async def opened(tmp_path):
    """A real headless browser on a fresh profile; skipped where Chromium cannot start (no browser installed, a sandbox)."""
    browser = agent_browser.AgentBrowser(tmp_path / "profile")
    try:
        await asyncio.wait_for(browser.start(), 30)
    except Exception as error:  # noqa: BLE001 - the environment, not the code
        pytest.skip(f"Chromium could not start here: {error}")
    yield browser
    await browser.close()


async def first_frame(browser, tab=None, width=320) -> bytes:
    async for frame in browser.frames(tab, width):
        return frame
    raise AssertionError("no frame")


async def test_the_browser_is_watched_driven_and_closed(opened, tmp_path):
    browser = opened
    assert browser.endpoint.startswith("http://127.0.0.1:") and browser.endpoint in agent_browser.OPEN
    assert [tab["id"] for tab in browser.tabs()] == ["t1"]  # Chromium's first tab
    page = browser.page(None)
    await page.goto(FORM)
    frame = await asyncio.wait_for(first_frame(browser), 10)
    assert frame[:2] == b"\xff\xd8"  # a JPEG
    # The person clicks the field (fractions of the frame) and types; the page gets it.
    await browser.input(None, {"kind": "down", "x": 250 / 1280, "y": 120 / 800, "button": "left", "clicks": 1})
    await browser.input(None, {"kind": "up", "x": 250 / 1280, "y": 120 / 800, "button": "left"})
    await browser.input(None, {"kind": "text", "text": "hi there"})
    await browser.input(None, {"kind": "key", "key": "Backspace"})
    assert await page.evaluate("o.textContent") == "hi ther"
    await browser.input(None, {"kind": "wheel", "x": 0.5, "y": 0.5, "dy": 100})
    await page.evaluate("document.oncontextmenu = (e) => { o.textContent = 'menu at ' + e.clientX; }")
    await browser.input(None, {"kind": "click", "x": 250 / 1280, "y": 120 / 800, "button": "right"})
    assert await page.evaluate("o.textContent") == "menu at 250"  # a right click reaches the page as one
    with pytest.raises(RunnerCoreError, match="Unknown input kind"):
        await browser.input(None, {"kind": "dance"})
    # A second tab: the newest is followed until the agent's results name another; a named tab is streamed as such.
    second = await browser.new_tab("data:text/html,<b>two</b>")
    assert second == {"id": "t2"} and browser.page(None) is browser.page("t2") and [tab["agent"] for tab in browser.tabs()] == [False, False]
    browser.seen("### Page\n- Page URL: " + page.url + "\n- Page Title: form")  # as Chrome spells the URL, which is how the tool result names it
    assert browser.page(None) is page and browser.tabs()[0]["agent"] is True
    shot = await browser.frame()  # the step's screenshot for the run: the followed tab
    assert base64.b64decode(shot["base64"])[:8] == b"\x89PNG\r\n\x1a\n" and (shot["width"], shot["height"]) == (1280, 800)
    assert await agent_browser.AgentBrowser(tmp_path / "unopened").frame() is None
    assert (await asyncio.wait_for(first_frame(browser, "t2"), 10))[:2] == b"\xff\xd8"
    await asyncio.sleep(0.3)  # titles are read after navigation
    assert browser.tabs()[1]["url"].startswith("data:text/html") and browser.tabs()[1]["title"] == ""
    await browser.close_tab("t2")
    with pytest.raises(RunnerCoreError) as missing:
        browser.page("t2")
    assert missing.value.code == "browser_tab_missing" and [tab["id"] for tab in browser.tabs()] == ["t1"]
    # Another client attaches over CDP, as Playwright's MCP server does, and sees the same tabs.
    port = int(browser.endpoint.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass
    await browser.close()
    assert browser.context is None and browser.endpoint not in agent_browser.OPEN
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


async def test_a_following_stream_moves_with_the_agent_and_a_named_one_ends_with_its_tab(opened):
    browser = opened
    first = browser.page(None)
    await first.goto("data:text/html,<b>one</b>")
    frames = browser.frames(None, 320)
    assert (await asyncio.wait_for(anext(frames), 10))[:2] == b"\xff\xd8"
    await browser.new_tab("data:text/html,<i>two</i>")  # the newest tab: the stream follows it
    for _ in range(20):
        await asyncio.wait_for(anext(frames), 10)
    assert browser.page(None) is browser.page("t2")
    await frames.aclose()
    named = browser.frames("t1", 320)
    assert (await asyncio.wait_for(anext(named), 10))[:2] == b"\xff\xd8"
    await browser.close_tab("t1")
    with pytest.raises(StopAsyncIteration):
        for _ in range(20):
            await asyncio.wait_for(anext(named), 10)


class FakeBrowser:
    def __init__(self):
        self.inputs: list[dict] = []
        self.actions: list[tuple] = []

    def wire(self):
        return {"endpoint": "http://127.0.0.1:1", "viewport": {"width": 1280, "height": 800}, "tabs": [{"id": "t1", "title": "One", "url": "https://one", "agent": True}]}

    async def frames(self, tab, max_width):
        self.actions.append(("frames", tab, max_width))
        yield b"\xff\xd8one"
        yield b"\xff\xd8two"

    async def input(self, tab, event):
        self.inputs.append({"tab": tab, **event})

    async def new_tab(self, url):
        self.actions.append(("new", url))
        return {"id": "t2"}

    async def close_tab(self, tab):
        self.actions.append(("close", tab))

    async def go(self, tab, direction):
        self.actions.append((direction, tab))


async def test_the_monitor_reads_and_drives_the_browser_through_the_routes(tmp_path, monkeypatch):
    live, _client, _ = make_manager(tmp_path / "data")
    fake = FakeBrowser()
    monkeypatch.setattr(live, "agent_browser", lambda: fake)
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/session/browser")).json()["tabs"][0]["id"] == "t1"
        streamed = await http.get("/api/live/session/browser/stream", params={"tab": "t1", "width": 4000})
        assert streamed.headers["content-type"].startswith("multipart/x-mixed-replace") and fake.actions[-1] == ("frames", "t1", 1280)
        assert streamed.content == b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n\xff\xd8one\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 5\r\n\r\n\xff\xd8two\r\n--frame\r\n"
        assert (await http.post("/api/live/session/browser/input", json={"kind": "key", "key": "Enter"})).json() == {"ok": True}
        assert fake.inputs == [{"tab": None, "kind": "key", "key": "Enter"}]
        assert (await http.post("/api/live/session/browser/tabs", json={"action": "new", "url": "https://two"})).json() == {"id": "t2"}
        assert (await http.post("/api/live/session/browser/tabs", json={"action": "close", "tab": "t1"})).json() == {"closed": "t1"}
        assert (await http.post("/api/live/session/browser/tabs", json={"action": "back"})).json() == {"went": "back"}  # the followed tab
        assert (await http.post("/api/live/session/browser/tabs", json={"action": "forward", "tab": "t2"})).json() == {"went": "forward"}
        assert (await http.post("/api/live/session/browser/tabs", json={"action": "dance"})).status_code == 400
        assert fake.actions[-4:] == [("new", "https://two"), ("close", "t1"), ("back", None), ("forward", "t2")]


async def test_without_a_session_or_a_browser_the_monitor_is_told(tmp_path):
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/session/browser")).json()["code"] == "live_session_inactive"


async def test_logins_survive_the_session(tmp_path):
    """The profile is kept between sessions: a cookie set in one browser is there in the next on the same profile."""
    first = agent_browser.AgentBrowser(tmp_path / "profile")
    try:
        await asyncio.wait_for(first.start(), 30)
    except Exception as error:  # noqa: BLE001 - the environment, not the code
        pytest.skip(f"Chromium could not start here: {error}")
    assert first.context is not None
    await first.context.add_cookies([{"name": "session", "value": "logged-in", "domain": "example.com", "path": "/", "expires": 4102444800}])
    await first.close()
    second = agent_browser.AgentBrowser(tmp_path / "profile")
    await asyncio.wait_for(second.start(), 30)
    try:
        assert second.context is not None
        assert [(cookie["name"], cookie["value"]) for cookie in await second.context.cookies("https://example.com")] == [("session", "logged-in")]
    finally:
        await second.close()
