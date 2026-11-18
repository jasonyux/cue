"""Observation sources. Each returns the same Snapshot shape so the digest and proposer never change.

Today: the frontmost app's focused window (where keystrokes go) plus, when it differs, the window under the
mouse pointer (where the user may be reading or scrolling without clicking). Later candidates: a
notification-driven event tail, screenpipe, or lifetrace, each as another Source.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from PIL import Image

from ..permissions import Level, Permissions, Reason, address, browser
from ..storage.records import Snapshot
from ..utils.time import timestamp

__all__ = ["AccessibilitySource", "Snapshot", "Source", "snapshot_hash"]


def snapshot_hash(app: str, window: str, tree: str, pointer: str = "", selection: str = "", around: str = "") -> str:
    """The look's identity: a new selection, or the text cursor moved, is a new look, like new text."""
    seen = f"{app}\n{window}\n{tree}\n{pointer}" + (f"\n{selection}" if selection else "") + (f"\n{around}" if around else "")  # without them: the hash it always had
    return hashlib.sha1(seen.encode("utf-8", errors="replace")).hexdigest()[:16]


class Source(Protocol):
    def snapshot(self, again: Snapshot | None = None) -> Snapshot | None: ...  # None: nothing in front worth a look (the console itself)

    def screenshot(self) -> Image.Image | None: ...


CONSOLE = ("Cue Console", "ComputerPilot Console")  # the console's own title (console/app/layout.tsx) and the one before it, which an app installed then keeps: as a tab or an installed web app, never looked at or asked about


def onscreen_windows() -> list[dict[str, Any]]:
    """Normal-layer windows currently on screen, front to back: app, title, pid, and bounds in points."""
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    )
    result = []
    for entry in windows or []:
        if entry.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        result.append({"app": str(entry.get("kCGWindowOwnerName") or ""), "title": str(entry.get("kCGWindowName") or ""),
                       "pid": int(entry.get("kCGWindowOwnerPID") or 0),
                       "bounds": (float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
                                  float(bounds.get("Width", 0)), float(bounds.get("Height", 0)))})
    return result


def blurred_glimpse(pid: int, width: int = 240) -> Image.Image | None:
    """The app's front window as the monitor may show it while a look is blocked: taken now, shrunk and blurred past reading
    before it leaves this process, written nowhere. By the look's pid, since the window list is always current while
    AppKit's frontmost app is not in a process that never pumps its run loop. None when it has no window on screen."""
    from PIL import ImageFilter

    from ...desktop.ax.capture import capture_display_region

    window = next((item for item in onscreen_windows() if item["pid"] == pid), None)
    if window is None:
        return None
    image = capture_display_region(window["bounds"], show_cursor=False)
    image.thumbnail((width, width))
    return image.filter(ImageFilter.GaussianBlur(6))


def window_under_pointer(windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The topmost window under the mouse, or None."""
    from AppKit import NSEvent, NSScreen

    location = NSEvent.mouseLocation()
    screens = NSScreen.screens()
    if not screens:
        return None
    # AppKit's origin is bottom-left of the main screen; the window list uses top-left.
    x, y = float(location.x), float(screens[0].frame().size.height - location.y)
    for window in windows:  # front to back
        left, top, width, height = window["bounds"]
        if left <= x <= left + width and top <= y <= top + height:
            return window
    return None


class AccessibilitySource:
    """The frontmost app's focused (key) window as an accessibility tree, plus the window under the pointer."""

    def __init__(self, *, max_elements: int = 400, permissions: Permissions | None = None) -> None:
        from ...desktop.ax import AccessibilityDesktop

        self.max_elements = max_elements
        self.permissions = permissions or Permissions()  # replaced by the recorder when settings.yaml changes
        self.desktop = AccessibilityDesktop(settle_seconds=0.0, screenshots=False)
        self.behind: Image.Image | None = None  # the pixels of a window looked at behind the front one, for the next screenshot()

    def allowed(self, name: str, bundle: str) -> bool:
        return self.permissions.level(name, bundle) != Level.BLOCK

    def snapshot(self, again: Snapshot | None = None) -> Snapshot | None:
        """A look at the app in front; or, with ``again`` (a stub whose app a rule now allows), at that app by its pid, wherever
        its window is: the answer was given on the console, and the person sees the app it was about."""
        from ...desktop.ax.client import frontmost_app, pump_run_loop

        if again is not None:
            name, bundle, pid = again.app, again.bundle or "", int(again.pid or 0)
        else:
            pump_run_loop()
            app = frontmost_app()
            if app is None:
                return Snapshot(ts=timestamp(), app="", error="No frontmost application.")
            name = str(app.localizedName() or "")
            bundle = str(app.bundleIdentifier() or "")
            pid = int(app.processIdentifier())
        try:
            windows = onscreen_windows()
        except Exception:  # noqa: BLE001 - window-list lookups are best effort
            windows = []
        front = next((window["title"] for window in windows if window["pid"] == pid), "")
        if name in CONSOLE or front.startswith(CONSOLE):  # the console itself: not a look, whatever the rules say
            return None
        def blocked(reason: Reason) -> Snapshot:  # the app and why, nothing of its content; hashed so repeated ticks collapse like an unchanged screen
            return Snapshot(ts=timestamp(), app=name, bundle=bundle, pid=pid, excluded=True, reason=reason, hash=snapshot_hash(name, "", reason))

        level, reason = self.permissions.decide(name, bundle)
        if level == Level.BLOCK:
            return blocked(reason)
        try:
            state = self.desktop.get_state(pid, screenshot=again is not None, max_elements=self.max_elements)  # behind the front window: its own pixels
        except Exception as error:  # noqa: BLE001 - apps without windows or trees are normal; keep observing
            return Snapshot(ts=timestamp(), app=name, bundle=bundle, pid=pid, error=f"{type(error).__name__}: {error}"[:300])
        self.behind = state.screenshot if again is not None else None
        site = address(state.elements) if browser(bundle) else None  # elsewhere a URL in a text field is content
        if site:
            level, reason = self.permissions.decide(name, bundle, site)  # the app itself was not blocked, so a block here is a site rule
            if level == Level.BLOCK:
                return blocked(reason)
        pointer_app = pointer_window = None
        visible: list[str] = []
        try:
            under = window_under_pointer(windows)
        except Exception:  # noqa: BLE001 - best effort
            under = None
        elsewhere = under is not None and (under["pid"] != pid or (under["title"] and under["title"] != state.window_title))
        if under is not None and elsewhere and self.allowed(under["app"], ""):
            pointer_app, pointer_window = under["app"], under["title"]
        for window in windows:
            if window["pid"] == pid and window["title"] == state.window_title:
                continue
            label = f"{window['app']} — {window['title']}" if window["title"] else window["app"]
            if self.allowed(window["app"], "") and label not in visible and len(visible) < 12:
                visible.append(label)
        pointer = f"{pointer_app or ''}\n{pointer_window or ''}"
        try:
            selection = self.desktop.selected_text(pid)  # what the person marked: the strongest cue of what they look at
        except Exception:  # noqa: BLE001 - best effort; many apps expose no selection
            selection = ""
        try:
            around = self.desktop.text_around(pid)  # the lines around it, or around the cursor: an editor's tree shows little of its text
        except Exception:  # noqa: BLE001 - best effort
            around = ""
        return Snapshot(ts=timestamp(), app=name, bundle=bundle, pid=pid, window=state.window_title, tree=state.text,
                        hash=snapshot_hash(name, state.window_title, state.text, pointer, selection, around),
                        pointer_app=pointer_app, pointer_window=pointer_window, visible=visible or None, selection=selection or None,
                        around=around or None)

    def screenshot(self) -> Image.Image | None:
        # The user's view of the focused window, cut from its display: overlaps and pointer included, no window lookup.
        # After a look behind the front window, that window's own pixels: a display cut would show what covers it.
        if self.behind is not None:
            image, self.behind = self.behind, None
            return image
        return self.desktop.visible_snapshot()
