"""Pointer-free control of macOS apps.

Observation: the target app's key window as an indexed accessibility tree plus a
window-only screenshot. Actions: accessibility actions on those elements (press,
set value, focus) and, when an element offers no action, mouse and keyboard
events posted directly to the app's process. Nothing here moves the system
cursor or changes which app has keyboard focus, so the user keeps working.
With ``rules`` (the person's permissions, live/permissions.py), every call
is checked first: a read needs ``look``, an action ``use``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, Union

from PIL import Image

from .capture import ax_window_id, capture_display_region, capture_window, find_window_id
from .keys import key_code_for_character, parse_chord
from .tree import Element, Frame, Reader, clip, render, serialize_tree

AX_SUCCESS = 0
SELECTION_CHARS = 500  # the most of a text selection a look keeps
AROUND_LINES = 6      # text_around: lines kept on each side of the selection or the text cursor,
AROUND_CHARS = 600    # and at most this many characters on each side
AX_POINT = 1
AX_SIZE = 2
AX_RANGE = 4  # kAXValueCFRangeType
MOUSE = {
    "left": (1, 2, 6, 0),   # down, up, dragged, button
    "right": (3, 4, 7, 1),
    "middle": (25, 26, 27, 2),
}
MOUSE_MOVED = 5
CLICK_STATE_FIELD = 1
SCROLL_UNIT_LINE = 1


AppRef = Union[str, int, "RunningApp", None]

# Bundle ids for apps the model is likely to name; everything else resolves through NSWorkspace.
KNOWN_BUNDLE_IDS = {
    "system settings": "com.apple.systempreferences", "system preferences": "com.apple.systempreferences",
    "settings": "com.apple.systempreferences", "finder": "com.apple.finder", "calculator": "com.apple.calculator",
    "notes": "com.apple.Notes", "safari": "com.apple.Safari", "textedit": "com.apple.TextEdit",
    "calendar": "com.apple.iCal", "mail": "com.apple.mail", "messages": "com.apple.MobileSMS",
    "terminal": "com.apple.Terminal", "reminders": "com.apple.reminders", "music": "com.apple.Music",
    "photos": "com.apple.Photos", "preview": "com.apple.Preview", "maps": "com.apple.Maps",
    "contacts": "com.apple.Contacts", "app store": "com.apple.AppStore", "activity monitor": "com.apple.ActivityMonitor",
    "chrome": "com.google.Chrome", "google chrome": "com.google.Chrome", "chromium": "org.chromium.Chromium",
    "slack": "com.tinyspeck.slackmacgap", "vs code": "com.microsoft.VSCode", "code": "com.microsoft.VSCode",
    "visual studio code": "com.microsoft.VSCode",
}


def pump_run_loop(seconds: float = 0.05) -> None:
    """NSWorkspace's running-app list and other AppKit state only refresh while the run loop runs."""
    from Foundation import NSDate, NSRunLoop

    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


class Rules(Protocol):
    """The person's permissions as ``ax`` asks them: why ``need`` (look or use) is refused for an app, with the elements of its
    window when they are known (a browser's page has rules of its own), or None; and an ask for more, answered on the console."""

    def refusal(self, name: str, bundle: str, elements: Sequence[Element] | None, need: str) -> str | None: ...

    def ask(self, name: str, bundle: str, need: str, why: str) -> str: ...

    def cell_done(self) -> None: ...  # a cell of model code ended: what was allowed once for it is over


class AccessibilityError(RuntimeError):
    """A recoverable problem the model can correct (wrong index, missing window, unsupported action)."""


@dataclass(frozen=True)
class RunningApp:
    pid: int
    name: str
    bundle_id: str
    ref: Any

    def __str__(self) -> str:
        return f"{self.name} ({self.bundle_id or 'no bundle id'}, pid {self.pid})"


@dataclass
class AppState:
    app: str
    pid: int
    window_title: str
    window_frame: Frame
    text: str
    screenshot: Image.Image | None
    elements: list[Element]

    @property
    def size(self) -> tuple[int, int]:
        return (int(self.window_frame[2]), int(self.window_frame[3]))

    def find(self, needle: str) -> str:
        """Lines of the tree whose text contains ``needle`` (case-insensitive)."""
        needle = needle.lower()
        return "\n".join(line for line in self.text.splitlines()[1:] if needle in line.lower()) or "(no matches)"

    def __repr__(self) -> str:
        return f"AppState({self.app!r}, window={self.window_title!r}, {len(self.elements)} elements, size={self.size})"


class MacReader(Reader):
    def __init__(self) -> None:
        import ApplicationServices as services

        self.services = services

    def attribute(self, element: Any, name: str) -> Any:
        error, value = self.services.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == AX_SUCCESS else None

    def children(self, element: Any) -> list[Any]:
        value = self.attribute(element, "AXChildren")
        return list(value) if value else []

    def frame(self, element: Any) -> Frame | None:
        position = self.attribute(element, "AXPosition")
        size = self.attribute(element, "AXSize")
        if position is None or size is None:
            return None
        ok_point, point = self.services.AXValueGetValue(position, AX_POINT, None)
        ok_size, extent = self.services.AXValueGetValue(size, AX_SIZE, None)
        if not ok_point or not ok_size:
            return None
        return (float(point.x), float(point.y), float(extent.width), float(extent.height))

    def parameterized(self, element: Any, name: str, parameter: Any) -> Any:
        error, value = self.services.AXUIElementCopyParameterizedAttributeValue(element, name, parameter, None)
        return value if error == AX_SUCCESS else None

    def text_range(self, element: Any, name: str) -> tuple[int, int] | None:
        """A range attribute (AXSelectedTextRange, AXVisibleCharacterRange) as (location, length)."""
        value = self.attribute(element, name)
        if value is None:
            return None
        ok, span = self.services.AXValueGetValue(value, AX_RANGE, None)
        if not ok:
            return None
        location, length = span if isinstance(span, tuple) else (span.location, span.length)  # PyObjC gives a CFRange as a tuple
        return int(location), int(length)

    def string_for_range(self, element: Any, location: int, length: int) -> str | None:
        """The element's text in [location, location + length), without reading all of it (a terminal's scrollback)."""
        value = self.parameterized(element, "AXStringForRange", self.services.AXValueCreate(AX_RANGE, (location, length)))
        return None if value is None else str(value)

    def actions(self, element: Any) -> list[str]:
        error, names = self.services.AXUIElementCopyActionNames(element, None)
        return [str(name) for name in names] if error == AX_SUCCESS and names else []

    def settable(self, element: Any, name: str) -> bool:
        error, value = self.services.AXUIElementIsAttributeSettable(element, name, None)
        return error == AX_SUCCESS and bool(value)

    def set(self, element: Any, name: str, value: Any) -> bool:
        return bool(self.services.AXUIElementSetAttributeValue(element, name, value) == AX_SUCCESS)

    def perform(self, element: Any, action: str) -> bool:
        return bool(self.services.AXUIElementPerformAction(element, action) == AX_SUCCESS)

    def element_at(self, app: Any, x: float, y: float) -> Any:
        error, element = self.services.AXUIElementCopyElementAtPosition(app, float(x), float(y), None)
        return element if error == AX_SUCCESS else None


class AccessibilityDesktop:
    """The ``ax`` global exposed to model code."""

    def __init__(self, *, settle_seconds: float = 0.7, screenshots: bool = True, rules: Rules | None = None) -> None:
        self.settle_seconds = settle_seconds
        self.screenshots = screenshots
        self.rules = rules
        self.reader = MacReader()
        self._states: dict[int, AppState] = {}
        self._web_enabled: set[int] = set()
        self._last_pid: int | None = None
        self._last_action_at = 0.0

    # ----- permissions and apps -------------------------------------------------

    @staticmethod
    def check_permissions() -> None:
        import ApplicationServices as services
        import Quartz

        if not services.AXIsProcessTrusted():
            raise RuntimeError(
                "macOS Accessibility permission is required. Enable the app launching the runner (Terminal or Codex) "
                "in System Settings > Privacy & Security > Accessibility, then retry."
            )
        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError(
                "macOS Screen Recording permission is required. Enable the app launching the runner in "
                "System Settings > Privacy & Security > Screen Recording, then restart it."
            )

    def list_apps(self) -> list[dict[str, Any]]:
        """Running apps with a user interface: name, bundle id, and pid."""
        from AppKit import NSWorkspace

        pump_run_loop()
        apps = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != 0 or not app.localizedName():
                continue
            apps.append({"name": str(app.localizedName()), "bundle_id": str(app.bundleIdentifier() or ""),
                         "pid": int(app.processIdentifier())})
        return sorted(apps, key=lambda item: item["name"].lower())

    def launch(self, app: str, wait_seconds: float = 12.0) -> RunningApp:
        """Start an app by name or bundle id without bringing it to the front, then wait for a window."""
        from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration

        existing = self._find_running(app)
        if existing is None:
            self._permit(app, KNOWN_BUNDLE_IDS.get(app.strip().lower(), app), "use")  # as named, before it starts
            workspace = NSWorkspace.sharedWorkspace()
            url = workspace.URLForApplicationWithBundleIdentifier_(KNOWN_BUNDLE_IDS.get(app.strip().lower(), app))
            if url is None:
                path = workspace.fullPathForApplication_(app)
                if path is None:
                    raise AccessibilityError(f"No installed app matches {app!r}. Use ax.list_apps() or a bundle id.")
                from Foundation import NSURL

                url = NSURL.fileURLWithPath_(path)
            configuration = NSWorkspaceOpenConfiguration.configuration()
            configuration.setActivates_(False)
            workspace.openApplicationAtURL_configuration_completionHandler_(url, configuration, None)
            deadline = time.monotonic() + wait_seconds
            while existing is None and time.monotonic() < deadline:
                time.sleep(0.25)
                existing = self._find_running(app)
            if existing is None:
                raise AccessibilityError(f"{app!r} did not start within {wait_seconds:g}s.")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self._window(existing, required=False) is not None:
                break
            time.sleep(0.25)
        self._mark_action()
        self._permit(existing.name, existing.bundle_id, "use")
        return existing

    def ask(self, app: str, level: str = "use", why: str = "") -> str:
        """Ask the person to allow ``level`` (look or use) on the app, saying what for; waits for their answer on the console."""
        if self.rules is None:
            return "No one to ask: this desktop has no permissions."
        running = self._find_running(app)
        name, bundle = (running.name, running.bundle_id) if running is not None else (app, KNOWN_BUNDLE_IDS.get(app.strip().lower(), ""))
        return self.rules.ask(name, bundle, level, why)

    def _find_running(self, app: str | int) -> RunningApp | None:
        import ApplicationServices as services
        from AppKit import NSWorkspace

        candidates = []
        wanted = str(app).strip().lower()
        wanted_bundle = KNOWN_BUNDLE_IDS.get(wanted, wanted)
        pump_run_loop()
        for running in NSWorkspace.sharedWorkspace().runningApplications():
            name = str(running.localizedName() or "")
            bundle = str(running.bundleIdentifier() or "")
            pid = int(running.processIdentifier())
            if isinstance(app, int) or wanted.isdigit():
                score = 0 if pid == int(app) else None
            elif bundle.lower() in (wanted, wanted_bundle):
                score = 0
            elif name.lower() == wanted:
                score = 1
            elif wanted and (name.lower().startswith(wanted) or wanted in name.lower()):
                score = 2
            else:
                score = None
            if score is not None:
                candidates.append((score, 0 if running.activationPolicy() == 0 else 1, pid, name, bundle))
        if not candidates:
            return None
        _, _, pid, name, bundle = min(candidates)
        return RunningApp(pid=pid, name=name, bundle_id=bundle, ref=services.AXUIElementCreateApplication(pid))

    def _resolve(self, app: AppRef, need: str = "use") -> RunningApp:
        """The app, resolved; refused with the reason when the person's rules keep it from being read (``need`` look) or acted in (use)."""
        if isinstance(app, RunningApp):
            running = app
        else:
            if app is None:
                if self._last_pid is None:
                    raise AccessibilityError("Name the app, for example ax.get_state('System Settings').")
                app = self._last_pid
            found = self._find_running(app)
            if found is None and not (isinstance(app, str) and not app.isdigit()):
                raise AccessibilityError(f"No running app matches {app!r}. Use ax.list_apps() or ax.launch(name).")
            running = found if found is not None else self.launch(str(app))
        state = self._states.get(running.pid) if need == "use" else None  # an action is on the page its indexes came from; a read checks the page it reads
        self._permit(running.name, running.bundle_id, need, state.elements if state is not None else None)
        return running

    def _permit(self, name: str, bundle: str, need: str, elements: Sequence[Element] | None = None) -> None:
        if self.rules is not None and (why := self.rules.refusal(name, bundle, elements, need)):
            raise AccessibilityError(why)

    def _enable_web_accessibility(self, app: RunningApp) -> None:
        # Chromium, Electron, and WebKit expose their trees only after a client asks.
        if app.pid in self._web_enabled:
            return
        self._web_enabled.add(app.pid)
        changed = False
        for attribute in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            changed = self.reader.set(app.ref, attribute, True) or changed
        if changed:
            time.sleep(0.4)

    def selected_text(self, app: RunningApp | int) -> str:
        """The text selected in the app's focused element, "" when there is none or it cannot be read: a field's own
        selection, else a web page's, which Chromium and WebKit keep as a text-marker range on the page, not as
        AXSelectedText. Reading only: nothing is focused or changed."""
        running = app if isinstance(app, RunningApp) else self._find_running(app)
        element = self.reader.attribute(running.ref, "AXFocusedUIElement") if running is not None else None
        text = str(self.reader.attribute(element, "AXSelectedText") or "") if element is not None else ""
        parameterized = getattr(self.reader, "parameterized", None)
        for _ in range(40):  # the focused element, then its ancestors up to the page or the window
            if text or element is None or parameterized is None:
                break
            marker = self.reader.attribute(element, "AXSelectedTextMarkerRange")
            if marker is not None:
                text = str(parameterized(element, "AXStringForTextMarkerRange", marker) or "")
            if self.reader.attribute(element, "AXRole") in ("AXWebArea", "AXWindow"):
                break
            element = self.reader.attribute(element, "AXParent")
        return clip(text, SELECTION_CHARS)

    def text_around(self, app: RunningApp | int) -> str:
        """The lines around the selection, or around the text cursor when nothing is selected, in the app's focused text
        element (an editor, a text field, a terminal): the selection between >>> and <<<, the cursor as >>><<<. "" when
        the element keeps no text range (a web page keeps its selection as text markers) or hides its text. Reading only."""
        running = app if isinstance(app, RunningApp) else self._find_running(app)
        element = self.reader.attribute(running.ref, "AXFocusedUIElement") if running is not None else None
        text_range = getattr(self.reader, "text_range", None)
        if element is None or text_range is None or self.reader.attribute(element, "AXRole") == "AXSecureTextField":
            return ""
        span = text_range(element, "AXSelectedTextRange")
        if span is None:
            return ""
        start, end = span[0], span[0] + span[1]
        low = max(0, start - AROUND_CHARS)
        total = self.reader.attribute(element, "AXNumberOfCharacters")
        high = end + AROUND_CHARS if not isinstance(total, int) else min(total, end + AROUND_CHARS)
        reading = getattr(self.reader, "string_for_range", None)
        text = reading(element, low, max(0, high - low)) if reading is not None else None
        if text is None:  # no ranged read: slice the whole value
            value = self.reader.attribute(element, "AXValue")
            text = value[low:high] if isinstance(value, str) else ""
        if not text.strip():
            return ""
        before, chosen, after = text[: start - low], text[start - low: end - low], text[end - low:]
        before = "\n".join(before.split("\n")[-(AROUND_LINES + 1):])  # the cursor's own line and the lines above it
        after = "\n".join(after.split("\n")[: AROUND_LINES + 1])
        return f"{before}>>>{chosen[:SELECTION_CHARS]}<<<{after}"

    def _window(self, app: RunningApp, *, required: bool = True) -> Any:
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            window = self.reader.attribute(app.ref, attribute)
            if window is not None:
                return window
        windows = self.reader.attribute(app.ref, "AXWindows") or []
        for window in windows:
            if self.reader.attribute(window, "AXMinimized") is not True:
                return window
        if required:
            raise AccessibilityError(f"{app} has no open window. Open one, or call ax.launch({app.name!r}).")
        return None

    # ----- observation ----------------------------------------------------------

    def get_state(self, app: AppRef = None, *, screenshot: bool | None = None,
                  max_elements: int = 700) -> AppState:
        """Capture the app's key window: indexed accessibility text and a window-only screenshot."""
        running = self._resolve(app, "look")
        self._settle()
        self._enable_web_accessibility(running)
        window = self._window(running)
        frame = self.reader.frame(window)
        if frame is None:
            raise AccessibilityError(f"Could not read the window frame of {running}.")
        title = str(self.reader.attribute(window, "AXTitle") or "")
        elements = serialize_tree(window, self.reader, window_frame=frame, max_elements=max_elements)
        self._permit(running.name, running.bundle_id, "look", elements)  # the page, in a browser
        truncated = len(elements) >= max_elements
        image = None
        note = ""
        if self.screenshots if screenshot is None else screenshot:
            window_id = ax_window_id(window) or find_window_id(running.pid, frame, title)
            if window_id is not None:
                try:
                    image = capture_window(window_id, (int(frame[2]), int(frame[3])))
                except Exception as error:  # noqa: BLE001 - the tree is the observation; a lost screenshot is a note
                    note = f" · screenshot unavailable: {clip(str(error), 160)}"
        header = (f'{running.name} · window "{clip(title, 80)}" · {frame[2]:.0f}x{frame[3]:.0f} · '
                  f"coordinates are window-relative points; [index] labels are valid until the next get_state{note}")
        state = AppState(app=running.name, pid=running.pid, window_title=title, window_frame=frame,
                         text=render(elements, header, truncated), screenshot=image, elements=elements)
        self._states[running.pid] = state
        self._last_pid = running.pid
        return state

    def visible_snapshot(self) -> Image.Image | None:
        """What the user sees in the last observed window's frame, straight from its display (pointer included)."""
        state = self.last_state()
        if state is None:
            return None
        try:
            return capture_display_region(state.window_frame, show_cursor=True)
        except Exception:  # noqa: BLE001 - observer screenshots are best effort
            return None

    def last_state(self) -> AppState | None:
        return self._states.get(self._last_pid) if self._last_pid is not None else None

    def snapshot(self, *, show_cursor: bool = False) -> Image.Image | None:
        """A fresh screenshot of the most recently observed app window, for the run timeline."""
        if self._last_pid is None:
            return None
        try:
            running = self._find_running(self._last_pid)
            if running is None:
                return None
            window = self._window(running, required=False)
            frame = self.reader.frame(window) if window is not None else None
            if frame is None:
                return None
            window_id = ax_window_id(window) or find_window_id(running.pid, frame, str(self.reader.attribute(window, "AXTitle") or ""))
            if window_id is None:
                return None
            return capture_window(window_id, (int(frame[2]), int(frame[3])), show_cursor=show_cursor)
        except Exception:  # noqa: BLE001 - timeline screenshots are best effort
            return None

    def _element(self, app: RunningApp, index: Any) -> Element:
        state = self._states.get(app.pid)
        if state is None:
            raise AccessibilityError(f"Call ax.get_state({app.name!r}) before using element indexes.")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(state.elements):
            raise AccessibilityError(f"Element index {index!r} is not in the latest state (0..{len(state.elements) - 1}).")
        return state.elements[index]

    def _window_origin(self, app: RunningApp) -> tuple[float, float]:
        state = self._states.get(app.pid)
        if state is not None:
            return (state.window_frame[0], state.window_frame[1])
        frame = self.reader.frame(self._window(app))
        if frame is None:
            raise AccessibilityError(f"Could not read the window frame of {app}.")
        return (frame[0], frame[1])

    def _screen_point(self, app: RunningApp, x: Any, y: Any) -> tuple[float, float]:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise AccessibilityError("Coordinates must be numbers relative to the window screenshot.")
        origin_x, origin_y = self._window_origin(app)
        return (origin_x + float(x), origin_y + float(y))

    def _element_screen_center(self, app: RunningApp, element: Element) -> tuple[float, float]:
        center = element.center()
        if center is None:
            raise AccessibilityError(f"Element [{element.index}] has no on-screen position; use another element.")
        origin_x, origin_y = self._window_origin(app)
        return (origin_x + center[0], origin_y + center[1])

    # ----- actions --------------------------------------------------------------

    def click(self, app: AppRef = None, index: int | None = None, *, x: float | None = None,
              y: float | None = None, button: str = "left", count: int = 1) -> str:
        """Click an element by index (accessibility press) or a window coordinate (event posted to the app)."""
        running = self._resolve(app)
        if button not in MOUSE:
            raise AccessibilityError("button must be 'left', 'right', or 'middle'.")
        if index is not None:
            element = self._element(running, index)
            if button == "left" and count == 1 and self.reader.perform(element.ref, "AXPress"):
                self._mark_action()
                return f"pressed [{index}] {element.role} {element.title!r}"
            if button == "right" and self.reader.perform(element.ref, "AXShowMenu"):
                self._mark_action()
                return f"opened menu of [{index}] {element.role} {element.title!r}"
            point = self._element_screen_center(running, element)
            self._post_click(running.pid, point, button, count)
            return f"clicked [{index}] {element.role} {element.title!r} at its center"
        if x is None or y is None:
            raise AccessibilityError("Pass an element index or both x and y.")
        point = self._screen_point(running, x, y)
        if button == "left" and count == 1:
            target = self.reader.element_at(running.ref, *point)
            if target is not None and "AXPress" in self.reader.actions(target) and self.reader.perform(target, "AXPress"):
                self._mark_action()
                role = self.reader.attribute(target, "AXRole")
                return f"pressed {role} at ({x}, {y})"
        self._post_click(running.pid, point, button, count)
        return f"clicked at ({x}, {y})"

    def double_click(self, app: AppRef = None, index: int | None = None, *, x: float | None = None,
                     y: float | None = None) -> str:
        return self.click(app, index, x=x, y=y, count=2)

    def right_click(self, app: AppRef = None, index: int | None = None, *, x: float | None = None,
                    y: float | None = None) -> str:
        return self.click(app, index, x=x, y=y, button="right")

    def focus(self, app: AppRef, index: int) -> str:
        running = self._resolve(app)
        element = self._element(running, index)
        if not self.reader.set(element.ref, "AXFocused", True) and not self.reader.perform(element.ref, "AXPress"):
            self._post_click(running.pid, self._element_screen_center(running, element), "left", 1)
        self._mark_action()
        return f"focused [{index}] {element.role} {element.title!r}"

    def set_value(self, app: AppRef, index: int, value: Any) -> str:
        """Replace an editable element's value, falling back to select-all and typing."""
        running = self._resolve(app)
        element = self._element(running, index)
        text = "" if value is None else str(value)
        if self.reader.settable(element.ref, "AXValue") and self.reader.set(element.ref, "AXValue", text):
            self._mark_action()
            return f"set [{index}] {element.role} value to {text!r}"
        self.focus(running, index)
        time.sleep(0.15)
        self.press_key(running, "cmd+a")
        if text:
            self.type_text(running, text)
        else:
            self.press_key(running, "backspace")
        return f"typed {text!r} into [{index}] {element.role}"

    def type_text(self, app: AppRef, text: str) -> str:
        """Type into whatever has keyboard focus inside the app (events go to that process only)."""
        running = self._resolve(app)
        if not isinstance(text, str):
            raise AccessibilityError("type_text expects a string.")
        for character in text:
            mapped = key_code_for_character(character)
            if mapped is not None and (character in "\n\t " or character.isascii() and character.isalnum()):
                self._post_key(running.pid, mapped[0], mapped[1])
            else:
                self._post_key(running.pid, 0, 0, character)
        return f"typed {len(text)} characters"

    def press_key(self, app: AppRef, chord: str) -> str:
        """Press a key or chord such as 'Return', 'Tab', 'cmd+a', or 'shift+Tab'."""
        running = self._resolve(app)
        try:
            code, flags = parse_chord(chord)
        except ValueError as error:
            raise AccessibilityError(str(error)) from error
        self._post_key(running.pid, code, flags)
        return f"pressed {chord}"

    def scroll(self, app: AppRef = None, index: int | None = None, *, x: float | None = None,
               y: float | None = None, direction: str = "down", amount: int = 5) -> str:
        """Scroll by ``amount`` lines at an element, a coordinate, or the window center."""
        running = self._resolve(app)
        direction = direction.lower()
        if direction not in ("up", "down", "left", "right"):
            raise AccessibilityError("direction must be up, down, left, or right.")
        if index is not None:
            point = self._element_screen_center(running, self._element(running, index))
        elif x is not None and y is not None:
            point = self._screen_point(running, x, y)
        else:
            origin_x, origin_y = self._window_origin(running)
            state = self._states.get(running.pid)
            width, height = state.size if state else (400, 300)
            point = (origin_x + width / 2, origin_y + height / 2)
        import Quartz

        lines = max(1, int(amount))
        vertical = {"up": lines, "down": -lines}.get(direction, 0)
        horizontal = {"left": lines, "right": -lines}.get(direction, 0)
        event = Quartz.CGEventCreateScrollWheelEvent(None, SCROLL_UNIT_LINE, 2, vertical, horizontal)
        Quartz.CGEventSetLocation(event, point)
        self._post(running.pid, event)
        return f"scrolled {direction} {lines} lines"

    def drag(self, app: AppRef, start: tuple[float, float], end: tuple[float, float], steps: int = 12) -> str:
        """Drag with the left button between two window coordinates."""
        running = self._resolve(app)
        import Quartz

        begin = self._screen_point(running, *start)
        finish = self._screen_point(running, *end)
        down, up, dragged, button = MOUSE["left"]
        self._post(running.pid, Quartz.CGEventCreateMouseEvent(None, MOUSE_MOVED, begin, button))
        self._post(running.pid, Quartz.CGEventCreateMouseEvent(None, down, begin, button))
        for step in range(1, max(2, steps) + 1):
            fraction = step / max(2, steps)
            point = (begin[0] + (finish[0] - begin[0]) * fraction, begin[1] + (finish[1] - begin[1]) * fraction)
            self._post(running.pid, Quartz.CGEventCreateMouseEvent(None, dragged, point, button))
            time.sleep(0.03)
        self._post(running.pid, Quartz.CGEventCreateMouseEvent(None, up, finish, button))
        return f"dragged from {tuple(start)} to {tuple(end)}"

    def actions(self, app: AppRef, index: int) -> list[str]:
        running = self._resolve(app, "look")
        return self.reader.actions(self._element(running, index).ref)

    def perform_action(self, app: AppRef, index: int, action: str) -> str:
        """Invoke a named accessibility action listed for the element (for example AXShowMenu, AXIncrement)."""
        running = self._resolve(app)
        element = self._element(running, index)
        if not self.reader.perform(element.ref, action):
            raise AccessibilityError(f"[{index}] does not support {action!r}; available: {self.reader.actions(element.ref)}")
        self._mark_action()
        return f"performed {action} on [{index}] {element.role} {element.title!r}"

    # Names from the Codex computer-use (Sky window) API, so its guidance applies unchanged.
    get_app_state = get_state
    perform_secondary_action = perform_action
    launch_app = launch

    # ----- event posting ---------------------------------------------------------

    def _post(self, pid: int, event: Any) -> None:
        import Quartz

        Quartz.CGEventPostToPid(pid, event)
        time.sleep(0.02)
        self._mark_action()

    def _post_click(self, pid: int, point: tuple[float, float], button: str, count: int) -> None:
        import Quartz

        down, up, _, mouse_button = MOUSE[button]
        self._post(pid, Quartz.CGEventCreateMouseEvent(None, MOUSE_MOVED, point, mouse_button))
        for click in range(1, max(1, int(count)) + 1):
            for kind in (down, up):
                event = Quartz.CGEventCreateMouseEvent(None, kind, point, mouse_button)
                Quartz.CGEventSetIntegerValueField(event, CLICK_STATE_FIELD, click)
                self._post(pid, event)

    def _post_key(self, pid: int, code: int, flags: int, text: str | None = None) -> None:
        import Quartz

        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, code, pressed)
            Quartz.CGEventSetFlags(event, flags)
            if text is not None:
                Quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
            self._post(pid, event)

    def _mark_action(self) -> None:
        self._last_action_at = time.monotonic()

    def _settle(self) -> None:
        remaining = self.settle_seconds - (time.monotonic() - self._last_action_at)
        if remaining > 0:
            time.sleep(remaining)


def frontmost_app() -> Any:
    from AppKit import NSWorkspace

    return NSWorkspace.sharedWorkspace().frontmostApplication()


def reactivate(app: Any) -> None:
    """Give focus back to the app the user was using before a window opened."""
    if app is not None:
        app.activateWithOptions_(1 << 1)


__all__ = ["AccessibilityDesktop", "AccessibilityError", "AppState", "RunningApp", "frontmost_app", "reactivate"]
