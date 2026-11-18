"""Screenshots for the accessibility client.

Two kinds, for two consumers:

* ``capture_display_region`` — what the user sees: the focused window's frame cut straight out of its display,
  overlapping windows and pointer included. Needs only the frame the accessibility element already gives, so
  there is no window lookup to get wrong. The observer uses this.
* ``capture_window`` — one window's own pixels even when other windows cover it. The agent uses this because
  its window usually sits behind the user's. It needs a window-server ID: ``ax_window_id`` maps the
  accessibility window to it directly (a private but long-stable call), and ``find_window_id`` is the public
  fallback that matches frames and titles.

ScreenCaptureKit is the supported capture API; older Quartz calls are fallbacks. All need Screen Recording.
"""

from __future__ import annotations

import threading
from typing import Any

from PIL import Image

Frame = tuple[float, float, float, float]
_ax_window_function: Any = None


def ax_window_id(element: Any) -> int | None:
    """Window-server ID of an accessibility window element, via the private ``_AXUIElementGetWindow``; None if unavailable."""
    global _ax_window_function
    try:
        import ctypes

        import objc

        if _ax_window_function is None:
            library = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            function = library._AXUIElementGetWindow
            function.restype = ctypes.c_int
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            _ax_window_function = function
        result = ctypes.c_uint32(0)
        if _ax_window_function(objc.pyobjc_id(element), ctypes.byref(result)) != 0 or result.value == 0:
            return None
        return int(result.value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def active_displays() -> list[tuple[int, Frame]]:
    """(display id, bounds) for every active display, in the same global point space as accessibility frames."""
    import Quartz

    _, identifiers, count = Quartz.CGGetActiveDisplayList(16, None, None)
    displays: list[tuple[int, Frame]] = []
    for identifier in list(identifiers or [])[:count]:
        bounds = Quartz.CGDisplayBounds(identifier)
        displays.append((int(identifier), (float(bounds.origin.x), float(bounds.origin.y),
                                           float(bounds.size.width), float(bounds.size.height))))
    return displays


def overlap(a: Frame, b: Frame) -> float:
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


def pick_display(frame: Frame, displays: list[tuple[int, Frame]]) -> tuple[int, Frame]:
    """The display holding the frame's center, else the one it overlaps most, else the first."""
    if not displays:
        raise RuntimeError("No active displays.")
    center_x, center_y = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
    for identifier, bounds in displays:
        if bounds[0] <= center_x < bounds[0] + bounds[2] and bounds[1] <= center_y < bounds[1] + bounds[3]:
            return identifier, bounds
    return max(displays, key=lambda item: overlap(frame, item[1]))


def list_windows(pid: int) -> list[dict[str, Any]]:
    """Normal-layer windows owned by ``pid`` with their screen bounds in points."""
    import Quartz

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    )
    windows = []
    for entry in info or []:
        if entry.get("kCGWindowOwnerPID") != pid or entry.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        windows.append({
            "id": int(entry["kCGWindowNumber"]),
            "title": str(entry.get("kCGWindowName") or ""),
            "frame": (float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
                      float(bounds.get("Width", 0)), float(bounds.get("Height", 0))),
            "onscreen": bool(entry.get("kCGWindowIsOnscreen", False)),
        })
    return windows


def onscreen_window_ids(pid: int) -> list[int]:
    """IDs of ``pid``'s normal-layer windows that are on screen, frontmost first (the option that guarantees z-order)."""
    import Quartz

    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    )
    return [int(entry["kCGWindowNumber"]) for entry in info or []
            if entry.get("kCGWindowOwnerPID") == pid and entry.get("kCGWindowLayer", 0) == 0]


def find_window_id(pid: int, frame: Frame | None, title: str = "") -> int | None:
    """Pick the window-server ID for an accessibility window.

    Both descriptions of a window come from the OS: the accessibility element gives a frame and a title, the
    window server gives the same for every window it draws. Selection order, most to least reliable:
    1. windows whose frame matches the accessibility frame (exact for a normal app, ambiguous only when several
       windows share one frame, such as many maximized editor windows on one display);
    2. among those, the one whose window-server title equals the accessibility title, if any has one;
    3. among those, the frontmost in the window server's z-order (the focused window of an app is normally its
       frontmost window);
    4. with no frame match at all, a title match anywhere, then the frontmost on-screen window.
    """
    windows = list_windows(pid)
    if not windows:
        return None
    by_id = {int(window["id"]): window for window in windows}
    z_order = [window_id for window_id in onscreen_window_ids(pid) if window_id in by_id]

    def frontmost(pool: list[dict[str, Any]]) -> int | None:
        ids = {int(window["id"]) for window in pool}
        for window_id in z_order:
            if window_id in ids:
                return window_id
        onscreen = [window for window in pool if window["onscreen"]]
        return int((onscreen or pool)[0]["id"]) if pool else None

    def titled(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [window for window in pool if title and window["title"] == title]

    def near(window: dict[str, Any]) -> bool:
        return frame is not None and sum(abs(a - b) for a, b in zip(window["frame"], frame)) < 8

    candidates = [window for window in windows if near(window)]
    if candidates:
        return frontmost(titled(candidates) or candidates)
    return frontmost(titled(windows) or windows)


def cgimage_to_pil(image: Any) -> Image.Image:
    import Quartz

    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    stride = Quartz.CGImageGetBytesPerRow(image)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))
    return Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", stride, 1).convert("RGB")


def ensure_core_graphics() -> None:
    """Open this process's window-server connection before ScreenCaptureKit needs it.

    Building an SCContentFilter asserts (``CGS_REQUIRE_INIT``) and aborts the whole process when no CoreGraphics
    call has initialized the connection yet. The window-list heuristic and the display capture make such a call
    first by accident; the direct accessibility → window ID bridge does not, so this is explicit.
    """
    import Quartz

    Quartz.CGMainDisplayID()


def capture_with_screencapturekit(window_id: int, size: tuple[int, int], timeout: float = 5.0, *,
                                  show_cursor: bool = False) -> Image.Image | None:
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    ensure_core_graphics()
    content: dict[str, Any] = {}
    ready = threading.Event()

    def got_content(shareable: Any, error: Any) -> None:
        content["value"], content["error"] = shareable, error
        ready.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
    if not ready.wait(timeout) or content.get("value") is None:
        return None
    window = next((item for item in content["value"].windows() if item.windowID() == window_id), None)
    if window is None:
        return None
    configuration = SCK.SCStreamConfiguration.alloc().init()
    configuration.setWidth_(max(1, int(size[0])))
    configuration.setHeight_(max(1, int(size[1])))
    configuration.setShowsCursor_(bool(show_cursor))
    if hasattr(configuration, "setIgnoreShadowsSingleWindow_"):
        configuration.setIgnoreShadowsSingleWindow_(True)
    result: dict[str, Any] = {}
    done = threading.Event()

    def got_image(image: Any, error: Any) -> None:
        result["value"], result["error"] = image, error
        done.set()

    window_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(window_filter, configuration, got_image)
    if not done.wait(timeout) or result.get("value") is None:
        return None
    return cgimage_to_pil(result["value"])


def capture_with_cgwindowlist(window_id: int) -> Image.Image | None:
    import Quartz

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
    )
    if image is None or Quartz.CGImageGetWidth(image) <= 1:
        return None
    return cgimage_to_pil(image)


def capture_display_with_screencapturekit(display_id: int, region: Frame, *, show_cursor: bool,
                                          timeout: float = 5.0) -> Image.Image | None:
    """One display, cropped to ``region`` (display-relative points)."""
    try:
        import Quartz
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    ensure_core_graphics()
    content: dict[str, Any] = {}
    ready = threading.Event()

    def got_content(shareable: Any, error: Any) -> None:
        content["value"], content["error"] = shareable, error
        ready.set()

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
    if not ready.wait(timeout) or content.get("value") is None:
        return None
    display = next((item for item in content["value"].displays() if int(item.displayID()) == display_id), None)
    if display is None:
        return None
    configuration = SCK.SCStreamConfiguration.alloc().init()
    configuration.setSourceRect_(Quartz.CGRectMake(*region))
    configuration.setWidth_(max(1, int(region[2])))
    configuration.setHeight_(max(1, int(region[3])))
    configuration.setShowsCursor_(bool(show_cursor))
    result: dict[str, Any] = {}
    done = threading.Event()

    def got_image(image: Any, error: Any) -> None:
        result["value"], result["error"] = image, error
        done.set()

    display_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
    SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(display_filter, configuration, got_image)
    if not done.wait(timeout) or result.get("value") is None:
        return None
    return cgimage_to_pil(result["value"])


def capture_display_region(frame: Frame, *, show_cursor: bool = True) -> Image.Image:
    """What the user sees inside ``frame`` (global points): the region of whichever display holds it."""
    display_id, bounds = pick_display(frame, active_displays())
    left, top = max(frame[0], bounds[0]), max(frame[1], bounds[1])
    right = min(frame[0] + frame[2], bounds[0] + bounds[2])
    bottom = min(frame[1] + frame[3], bounds[1] + bounds[3])
    if right - left < 1 or bottom - top < 1:
        raise RuntimeError("The window is off screen.")
    region: Frame = (left - bounds[0], top - bounds[1], right - left, bottom - top)
    image = capture_display_with_screencapturekit(display_id, region, show_cursor=show_cursor)
    if image is None:
        import Quartz

        create = getattr(Quartz, "CGDisplayCreateImageForRect", None)
        raw = create(display_id, Quartz.CGRectMake(*region)) if create is not None else None
        image = cgimage_to_pil(raw) if raw is not None else None
    if image is None:
        raise RuntimeError(
            "Display capture failed. Grant Screen Recording to the app launching the runner in "
            "System Settings > Privacy & Security > Screen Recording, then restart it."
        )
    if image.size != (int(region[2]), int(region[3])):
        image = image.resize((int(region[2]), int(region[3])), Image.Resampling.LANCZOS)
    return image


def capture_window(window_id: int, size: tuple[int, int], *, show_cursor: bool = False) -> Image.Image:
    """Pixels of one window. ``show_cursor`` draws the pointer when it is over the window (the observer wants it)."""
    image = capture_with_screencapturekit(window_id, size, show_cursor=show_cursor)
    if image is None:
        image = capture_with_cgwindowlist(window_id)
    if image is None:
        raise RuntimeError(
            "Window capture failed. Grant Screen Recording to the app launching the runner in "
            "System Settings > Privacy & Security > Screen Recording, then restart it."
        )
    if size[0] > 0 and size[1] > 0 and image.size != (int(size[0]), int(size[1])):
        image = image.resize((int(size[0]), int(size[1])), Image.Resampling.LANCZOS)
    return image
