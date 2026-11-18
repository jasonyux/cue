"""The agent browser: one headless Chrome per session (the installed one, else Playwright's Chromium), which the agent drives and the person watches on the monitor.

The backend launches it with its own Playwright on the profile kept between sessions (``data/agent-browser``, so logins
stay), with a CDP port that Playwright's MCP server attaches to from each run (``servers.py``); the run's MCP server is
just a client, gone with the run, and the browser closes with the session. The person sees it as a video stream:
``frames`` is a screencast of one tab, JPEG after JPEG, taken over CDP only while someone watches and detached when they
stop; ``input`` forwards their clicks, scrolling and typing to the tab. What the agent is on is followed from its tool
results (``seen``: every result of Playwright's tools names its page); a viewer that names no tab follows it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Error, Page, Playwright, ViewportSize, async_playwright

from ....errors import RunnerCoreError

VIEWPORT: ViewportSize = {"width": 1280, "height": 800}  # 16:10, the monitor's shape; the viewer maps its pointer onto it
PAGE_URL = re.compile(r"^- Page URL: (.+?)\s*$", re.MULTILINE)  # how a Playwright MCP tool result names its page
KEEPALIVE_S = 2.0  # a still page sends no frames; the last one is sent again, so a viewer that left is noticed
OPEN: dict[str, AgentBrowser] = {}  # endpoint → the browser, for the run that drives it (claude.py)


class AgentBrowser:
    def __init__(self, profile: Path) -> None:
        self.profile = profile
        self.endpoint = ""  # http://127.0.0.1:<port>, once started
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.ids: dict[Page, str] = {}  # each tab's id for the viewer, given in order of opening
        self.titles: dict[Page, str] = {}
        self.agent_page: Page | None = None  # the tab the agent's last action was on
        self.counter = itertools.count(1)

    async def start(self) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self._launch()
        active = self.profile / "DevToolsActivePort"  # Chrome writes its port there once it listens
        for _ in range(100):
            if active.exists() and active.read_text().strip():
                break
            await asyncio.sleep(0.1)
        self.endpoint = f"http://127.0.0.1:{active.read_text().splitlines()[0]}"
        self.context.on("page", self._opened)
        for page in self.context.pages:
            self._opened(page)
        OPEN[self.endpoint] = self

    async def _launch(self, user_agent: str | None = None) -> BrowserContext:
        """Chrome (the installed one, else Playwright's Chromium) in its new headless mode rather than the headless shell,
        without the marks sites block on: the ``webdriver`` flag, and "HeadlessChrome" in the user agent, which is read
        from a first launch and dropped for the second."""
        assert self.playwright is not None
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)  # a port of an earlier launch must not be read
        options: dict[str, Any] = {"headless": True, "viewport": VIEWPORT, "user_agent": user_agent, "ignore_default_args": ["--enable-automation"],
                                   "args": ["--remote-debugging-port=0", "--disable-blink-features=AutomationControlled"]}
        try:
            context = await self.playwright.chromium.launch_persistent_context(str(self.profile), channel="chrome", **options)
        except Error:  # no Google Chrome installed
            context = await self.playwright.chromium.launch_persistent_context(str(self.profile), channel="chromium", **options)
        if user_agent is None:
            page = context.pages[0] if context.pages else await context.new_page()
            seen = await page.evaluate("navigator.userAgent")
            if "HeadlessChrome" in seen:
                await context.close()
                return await self._launch(seen.replace("HeadlessChrome", "Chrome"))
        return context

    async def close(self) -> None:
        OPEN.pop(self.endpoint, None)
        context, playwright = self.context, self.playwright
        self.context = self.playwright = None
        if context is not None:
            await context.close()
        if playwright is not None:
            await playwright.stop()

    # ----- tabs -------------------------------------------------------------------------------------------------

    def _opened(self, page: Page) -> None:
        self.ids[page] = f"t{next(self.counter)}"
        self.titles[page] = ""
        page.on("framenavigated", lambda frame: self._navigated(page, frame))
        page.on("close", lambda _page: self._closed(page))
        self._navigated(page, page.main_frame)

    def _navigated(self, page: Page, frame: Any) -> None:
        if frame is page.main_frame:
            asyncio.ensure_future(self._title(page))

    async def _title(self, page: Page) -> None:
        with contextlib.suppress(Exception):  # the page may be gone, or still loading: the title comes next time
            self.titles[page] = await page.title()

    def _closed(self, page: Page) -> None:
        self.ids.pop(page, None)
        self.titles.pop(page, None)
        if self.agent_page is page:
            self.agent_page = None

    def seen(self, text: str) -> None:
        """A browser tool result of the agent: the page it names is where the agent is."""
        found = PAGE_URL.search(text)
        if found:
            self.agent_page = next((page for page in self.ids if page.url == found.group(1)), self.agent_page)

    def page(self, tab: str | None) -> Page:
        """The tab ``tab`` names, or with none the followed one: the agent's, else the newest."""
        pages = [page for page in self.ids if not page.is_closed()]
        if tab is not None:
            found = next((page for page in pages if self.ids[page] == tab), None)
        else:
            found = self.agent_page if self.agent_page in pages else (pages[-1] if pages else None)
        if found is None:
            raise RunnerCoreError("That tab is not open." if tab else "The agent browser has no tab open.", code="browser_tab_missing", status_code=404)
        return found

    async def frame(self) -> dict[str, Any] | None:
        """The followed tab as it is now, as the run files a screenshot (PNG and its size): the agent's step just done.
        None with no tab open, or one that cannot be captured mid-navigation."""
        try:
            page = self.page(None)
            return {"base64": base64.b64encode(await page.screenshot()).decode(), **(page.viewport_size or VIEWPORT)}
        except (RunnerCoreError, Error):
            return None

    def tabs(self) -> list[dict[str, Any]]:
        return [{"id": self.ids[page], "title": self.titles[page], "url": page.url, "agent": page is self.agent_page}
                for page in self.ids if not page.is_closed()]

    def wire(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "viewport": VIEWPORT, "tabs": self.tabs()}

    async def new_tab(self, url: str | None) -> dict[str, Any]:
        assert self.context is not None
        page = await self.context.new_page()
        if url:
            with contextlib.suppress(Exception):  # a bad URL still leaves the tab
                await page.goto(url)
        return {"id": self.ids[page]}

    async def close_tab(self, tab: str) -> None:
        await self.page(tab).close()

    async def go(self, tab: str | None, direction: str) -> None:
        """Back or forward in a tab's history, as the person asks from the sheet; nothing when there is nowhere to go."""
        page = self.page(tab)
        await (page.go_back() if direction == "back" else page.go_forward())

    # ----- what the person sees and does -------------------------------------------------------------------------

    async def frames(self, tab: str | None, max_width: int) -> AsyncIterator[bytes]:
        """JPEG frames of a tab as they change, the last one again every ``KEEPALIVE_S`` meanwhile. With no ``tab``
        it follows the agent: when the agent moves to another tab, so does the stream; a named tab ends it when closed."""
        while self.context is not None:
            try:
                page = self.page(tab)
            except RunnerCoreError:
                await asyncio.sleep(0.5)  # no tab yet: wait for one
                continue
            async for frame in self._screencast(page, max_width, follow=tab is None):
                yield frame
            if tab is not None:
                return

    async def _screencast(self, page: Page, max_width: int, follow: bool) -> AsyncIterator[bytes]:
        """One tab's frames until it closes or, when following, the agent is on another."""
        assert self.context is not None
        fresh = asyncio.Event()
        latest: list[bytes] = []  # the newest frame only: a viewer that falls behind skips to it rather than lagging
        cdp = await self.context.new_cdp_session(page)

        def on_frame(event: dict[str, Any]) -> None:
            latest[:] = [base64.b64decode(event["data"])]
            fresh.set()
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        try:
            await page.bring_to_front()  # a tab behind another renders slowly; the one watched is kept in front
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": max_width,
                                                    "maxHeight": round(max_width * VIEWPORT["height"] / VIEWPORT["width"])})
            last: bytes | None = None
            while not page.is_closed() and not (follow and self.page(None) is not page):
                try:
                    await asyncio.wait_for(fresh.wait(), KEEPALIVE_S)
                    fresh.clear()
                    last = latest[-1]
                except asyncio.TimeoutError:
                    pass
                if last is not None:
                    yield last
        except RunnerCoreError:
            return  # following, and no tab is open any more
        finally:
            with contextlib.suppress(Exception):  # the page may be gone already
                await cdp.detach()

    async def input(self, tab: str | None, event: dict[str, Any]) -> None:
        """One pointer or keyboard event of the viewer: ``x`` and ``y`` are fractions of the frame."""
        page = self.page(tab)
        kind = event.get("kind")
        if kind in ("move", "down", "up", "click", "wheel"):
            x, y = float(event["x"]) * VIEWPORT["width"], float(event["y"]) * VIEWPORT["height"]
            await page.mouse.move(x, y)
            if kind == "click":  # a right or middle button: pressed and released at once
                await page.mouse.click(x, y, button=event.get("button", "left"), click_count=int(event.get("clicks", 1)))
            elif kind == "down":
                await page.mouse.down(button=event.get("button", "left"), click_count=int(event.get("clicks", 1)))
            elif kind == "up":
                await page.mouse.up(button=event.get("button", "left"))
            elif kind == "wheel":
                await page.mouse.wheel(float(event.get("dx", 0)), float(event.get("dy", 0)))
        elif kind == "key":
            await page.keyboard.press(str(event["key"]))
        elif kind == "text":
            await page.keyboard.insert_text(str(event["text"]))
        else:
            raise RunnerCoreError(f"Unknown input kind {kind!r}.", code="invalid_request", status_code=400)
