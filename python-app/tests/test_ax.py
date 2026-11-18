"""Accessibility control mode: pure-Python pieces plus the worker, runtime, and scenario hooks (no macOS calls)."""
import asyncio
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from app.desktop import worker
from app.desktop.ax import desktop_mode
from app.desktop.ax.keys import FLAG_COMMAND, FLAG_SHIFT, KEY_CODES, key_code_for_character, parse_chord
from app.desktop.ax.session import DesktopSession
from app.desktop.ax.tree import Element, render, serialize_tree
from app.desktop.placeholder import PLACEHOLDER_SIZE, is_placeholder
from app.lab_catalog import instructions, list_scenarios
from app.responses_loop import build_code_tool_definitions
from app.scenario_runtime import RunExecutionContext, execute_scenario

# ----- keys -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("chord, expected", [
    ("Return", (KEY_CODES["return"], 0)),
    ("enter", (KEY_CODES["return"], 0)),
    ("cmd+a", (KEY_CODES["a"], FLAG_COMMAND)),
    ("Command+Shift+A", (KEY_CODES["a"], FLAG_COMMAND | FLAG_SHIFT)),
    ("shift+Tab", (KEY_CODES["tab"], FLAG_SHIFT)),
    ("A", (KEY_CODES["a"], FLAG_SHIFT)),
    ("!", (KEY_CODES["1"], FLAG_SHIFT)),
    ("cmd++", (KEY_CODES["="], FLAG_COMMAND | FLAG_SHIFT)),
    ("KP_0", (KEY_CODES["kp_0"], 0)),
])
def test_parse_chord(chord, expected):
    assert parse_chord(chord) == expected


@pytest.mark.parametrize("chord", ["", "cmd", "hyper+a", "cmd+", "nosuchkey"])
def test_parse_chord_rejects_malformed_input(chord):
    with pytest.raises(ValueError):
        parse_chord(chord)


def test_key_code_for_character_handles_whitespace_and_unicode():
    assert key_code_for_character("\n") == (KEY_CODES["return"], 0)
    assert key_code_for_character("x") == (KEY_CODES["x"], 0)
    assert key_code_for_character("é") is None


# ----- tree -----------------------------------------------------------------------------------------------


class FakeReader:
    """Nodes are dicts: {"role", "title", "value", "frame", "children", "actions", "subrole", ...}."""

    def attribute(self, element, name):
        return element.get({"AXRole": "role", "AXSubrole": "subrole", "AXTitle": "title", "AXDescription": "description",
                            "AXValue": "value", "AXPlaceholderValue": "placeholder", "AXEnabled": "enabled",
                            "AXFocused": "focused", "AXSelected": "selected"}.get(name, name))

    def children(self, element):
        return element.get("children", [])

    def frame(self, element):
        return element.get("frame")

    def actions(self, element):
        return element.get("actions", ["AXPress"])


def sample_window():
    return {
        "role": "AXWindow", "title": "General", "frame": (100, 200, 800, 600), "children": [
            {"role": "AXGroup", "frame": (100, 200, 800, 100), "children": [
                {"role": "AXStaticText", "value": "Login Items & Extensions", "frame": (120, 210, 300, 20)},
                {"role": "AXTextField", "title": "Search", "value": "", "placeholder": "Search", "frame": (500, 210, 200, 24),
                 "focused": True},
                {"role": "AXCheckBox", "subrole": "AXSwitch", "title": "Open at Login", "value": 1, "frame": (700, 260, 40, 20),
                 "actions": ["AXPress", "AXShowMenu"]},
                {"role": "AXButton", "title": "Offscreen", "frame": (2000, 2000, 10, 10)},
                {"role": "AXButton", "title": "Disabled", "frame": (120, 300, 60, 20), "enabled": False},
            ]},
        ],
    }


def test_serialize_tree_indexes_useful_elements_with_window_relative_frames():
    elements = serialize_tree(sample_window(), FakeReader(), window_frame=(100, 200, 800, 600))
    lines = [element.line() for element in elements]
    assert lines[0].startswith('[0] AXWindow "General" (0,0 800x600)')
    assert elements[1].role == "AXStaticText" and elements[1].title == "Login Items & Extensions"
    assert elements[1].frame == (20, 10, 300, 20)
    search = elements[2]
    assert search.role == "AXTextField" and search.focused and "placeholder='Search'" in search.line() and "focused" in search.line()
    switch = elements[3]
    assert switch.role == "AXSwitch" and switch.value == "1" and "actions=AXShowMenu" in switch.line()
    assert [element.title for element in elements] == ["General", "Login Items & Extensions", "Search", "Open at Login", "Disabled"]
    assert "disabled" in elements[4].line()
    assert elements[4].center() == (50, 110)  # (20 + 60/2, 100 + 20/2)


def test_serialize_tree_respects_limits_and_render_marks_truncation():
    window = {"role": "AXWindow", "title": "Big", "frame": (0, 0, 10, 10),
              "children": [{"role": "AXButton", "title": f"b{i}", "frame": (0, 0, 1, 1)} for i in range(50)]}
    elements = serialize_tree(window, FakeReader(), window_frame=(0, 0, 10, 10), max_elements=5)
    assert len(elements) == 5
    text = render(elements, "header", truncated=True)
    assert text.splitlines()[0] == "header" and text.splitlines()[-1].startswith("… (tree truncated")


def test_element_line_clips_long_text():
    element = Element(index=3, role="AXStaticText", title="x" * 500, value="", frame=None, ref=None, depth=2)
    line = element.line()
    assert line.startswith("[3]     AXStaticText") and len(line) < 220 and line.endswith('…"')


# ----- mode selection and worker protocol ---------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(None, "pyautogui"), ("", "pyautogui"), ("AX", "accessibility"),
                                             ("accessibility", "accessibility"), ("a11y", "accessibility")])
def test_desktop_mode_normalization(value, expected):
    assert desktop_mode(value) == expected


def test_desktop_mode_rejects_unknown_values():
    with pytest.raises(ValueError):
        desktop_mode("mouse")


def test_worker_resolve_mode_reads_environment(monkeypatch):
    monkeypatch.setenv("CUA_DESKTOP_MODE", "ax")
    if sys.platform == "darwin":
        assert worker.resolve_mode() == "accessibility"
    else:
        with pytest.raises(RuntimeError, match="only available on macOS"):
            worker.resolve_mode()
    monkeypatch.setenv("CUA_DESKTOP_MODE", "bogus")
    with pytest.raises(RuntimeError, match="CUA_DESKTOP_MODE"):
        worker.resolve_mode()
    monkeypatch.delenv("CUA_DESKTOP_MODE")
    assert worker.resolve_mode() == "pyautogui"


def test_worker_capture_operation_uses_the_observed_window_in_accessibility_mode():
    image = Image.new("RGB", (30, 20), (1, 2, 3))
    fake_ax = SimpleNamespace(snapshot=lambda: image,
                              last_state=lambda: SimpleNamespace(app="System Settings", window_title="General"))
    namespace = {"ax": fake_ax}
    requests = [{"id": 1, "operation": "capture"}, {"id": 2, "operation": "execute", "code": "log(ax.last_state().app)"}]
    with patch.object(worker, "emit") as emit:
        worker.serve_requests(namespace, io.StringIO("\n".join(map(json.dumps, requests))))
    capture = emit.call_args_list[0].args[0]["capture"]
    assert capture["app"] == "System Settings" and capture["title"] == "General"
    with Image.open(io.BytesIO(base64.b64decode(capture["image"]))) as decoded:
        assert decoded.size == (30, 20)
    assert emit.call_args_list[1].args[0]["output"][0]["text"] == "System Settings"


def test_worker_capture_operation_falls_back_to_pyautogui_and_survives_failures():
    fake_pyautogui = SimpleNamespace(screenshot=lambda: Image.new("RGB", (4, 4)))
    with patch.object(worker, "emit") as emit:
        worker.serve_requests({"pyautogui": fake_pyautogui}, io.StringIO(json.dumps({"id": 1, "operation": "capture"})))
    assert emit.call_args.args[0]["capture"]["image"]

    def broken():
        raise RuntimeError("no window server")

    with patch.object(worker, "emit") as emit:
        worker.serve_requests({"ax": SimpleNamespace(snapshot=broken, last_state=lambda: None)},
                              io.StringIO(json.dumps({"id": 1, "operation": "capture"})))
    assert emit.call_args.args[0]["capture"]["image"] is None
    assert "no window server" in emit.call_args.args[0]["capture"]["error"]


def test_worker_capture_without_a_desktop_returns_no_image():
    with patch.object(worker, "emit") as emit:
        worker.serve_requests({}, io.StringIO(json.dumps({"id": 1, "operation": "capture"})))
    assert emit.call_args.args[0] == {"id": 1, "capture": {"image": None}}


# ----- tool definition, instructions, and scenarios ---------------------------------------------------------


def test_tool_definition_describes_the_selected_mode():
    accessibility = build_code_tool_definitions("darwin", "accessibility")[0]
    pyautogui = build_code_tool_definitions("darwin")[0]
    assert "ax.get_state" in accessibility["parameters"]["properties"]["code"]["description"]
    assert "pyautogui" not in accessibility["parameters"]["properties"]["code"]["description"].split("There is no pyautogui")[0]
    assert "pyautogui.screenshot" in pyautogui["parameters"]["properties"]["code"]["description"]
    assert accessibility["description"] != pyautogui["description"]


def test_instructions_cover_every_mode_and_scenario():
    desktop_ax = instructions("desktop-login-items", mode="accessibility")
    assert "ax.launch(name)" in desktop_ax and "never toggle" in desktop_ax
    desktop_mouse = instructions("desktop-free-task")
    assert "subprocess.run(['open', '-a', name])" in desktop_mouse and "Calculator" not in desktop_mouse
    assert desktop_mouse == instructions("desktop-free-task", mode="pyautogui")


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop scenarios are listed on macOS only")
def test_desktop_scenarios_are_listed_with_an_existing_template():
    desktop = [item for item in list_scenarios() if item["labId"] == "desktop"]
    assert [item["id"] for item in desktop] == ["desktop-login-items", "desktop-free-task"]
    for item in desktop:
        assert Path(item["workspaceTemplatePath"]).is_dir()
        assert item["tags"] and item["defaultPrompt"]


# ----- desktop session and scenario runtime -------------------------------------------------------------------


async def test_desktop_session_writes_worker_screenshots_and_placeholders(tmp_path):
    png = io.BytesIO()
    Image.new("RGB", (50, 40)).save(png, format="PNG")
    captures = [{"image": base64.b64encode(png.getvalue()).decode(), "app": "Calculator", "title": "Calculator"},
                {"image": None, "app": "", "title": ""}]
    runtime = SimpleNamespace(capture=AsyncMock(side_effect=captures))
    session = DesktopSession(runtime=runtime, screenshot_dir=tmp_path / "shots", target_label="local")
    first = await session.capture_screenshot("desktop start")
    assert first["currentUrl"] == "desktop://Calculator" and first["pageTitle"] == "Calculator"
    assert session.viewport == {"width": 50, "height": 40}
    assert Path(first["path"]).name == "001-desktop-start.png" and Path(first["path"]).is_file()
    assert "placeholder" not in first
    second = await session.capture_screenshot("final")
    assert second["currentUrl"] == "desktop://Calculator" and "pageTitle" not in second and second["placeholder"] is True
    with Image.open(second["path"]) as placeholder:
        assert placeholder.size == PLACEHOLDER_SIZE
    assert is_placeholder(second) and not is_placeholder(first)
    # Records stored before the flag: only a timeline capture with the placeholder's canvas counts.
    assert is_placeholder({"source": "browser_preview", "imageWidth": 640, "imageHeight": 120})
    assert not is_placeholder({"source": "code_tool", "imageWidth": 640, "imageHeight": 120})
    assert await session.read_state() == {"currentUrl": "desktop://Calculator"}


@pytest.fixture
def desktop_flow(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    runtime = SimpleNamespace(close=AsyncMock(), mode="accessibility", platform="darwin", capture=AsyncMock())
    launch_worker = AsyncMock(return_value=runtime)
    loop = AsyncMock(return_value={"notes": ["done"]})
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.responses_loop.run_responses_code_loop", loop)
    monkeypatch.setattr("app.desktop.runtime.launch_python_runtime", launch_worker)
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-login-items", "labId": "desktop"}])
    context = RunExecutionContext(
        detail={"run": {"labId": "desktop", "scenarioId": "desktop-login-items", "browserMode": "headful",
                        "prompt": "Check login items", "model": "fixture", "maxResponseTurns": 8},
                "workspacePath": str(tmp_path)},
        signal=asyncio.Event(), screenshot_directory=tmp_path / "shots", emit_event=AsyncMock(),
        capture_screenshot=AsyncMock(), sync_browser_state=AsyncMock(), complete_run=AsyncMock())
    return SimpleNamespace(context=context, runtime=runtime, client=client, loop=loop)


async def test_desktop_scenario_runs_on_the_worker_session(desktop_flow):
    await execute_scenario(desktop_flow.context)
    session = desktop_flow.loop.call_args.kwargs["session"]
    assert isinstance(session, DesktopSession) and session.execution is desktop_flow.runtime
    prompt = desktop_flow.loop.call_args.kwargs["instructions"]
    assert "Accessibility API" in prompt and "never toggle" in prompt
    labels = [call.args[1] for call in desktop_flow.context.capture_screenshot.call_args_list]
    assert labels == ["desktop-start", "desktop-final"]
    desktop_flow.runtime.close.assert_awaited_once()
    desktop_flow.client.close.assert_awaited_once()
    desktop_flow.context.complete_run.assert_awaited_once_with(notes=["done"])


def test_ax_applies_the_persons_permissions_to_every_call(monkeypatch):
    """A read needs look, an action or a launch use, a browser's page its site rules; a refused call names the app and how to ask."""
    from app.desktop.ax import client
    from app.live.permissions import Level, Permissions, Rules

    rules = Rules(lambda: Permissions(Level.LOOK, {"Slack": Level.USE, "Messages": Level.BLOCK, "Safari": Level.USE},
                                      {"chase.com": Level.BLOCK, "docs.python.org": Level.LOOK}), ())
    desktop = object.__new__(client.AccessibilityDesktop)
    desktop.rules, desktop._states, desktop._last_pid, desktop._web_enabled = rules, {}, None, set()
    desktop.settle_seconds, desktop._last_action_at, desktop.screenshots = 0.0, 0.0, False
    desktop.reader = SimpleNamespace(frame=lambda window: (0, 0, 100, 50), attribute=lambda element, name: {"AXFocusedWindow": "win", "AXTitle": "page"}.get(name),
                                     set=lambda *args: False, perform=lambda *args: True)
    apps = [client.RunningApp(1, "Slack", "com.tinyspeck.slackmacgap", None), client.RunningApp(2, "Notes", "com.apple.Notes", None),
            client.RunningApp(3, "Messages", "com.apple.MobileSMS", None), client.RunningApp(4, "Safari", "com.apple.Safari", None)]
    desktop._find_running = lambda app: next((item for item in apps if app in (item.pid, item.name.lower(), item.name)), None)
    pages = iter(["https://online.chase.com/login", "https://docs.python.org/3/"])
    monkeypatch.setattr(client, "serialize_tree", lambda window, reader, window_frame, max_elements: [Element(0, "AXTextField", "", next(pages, ""), (0, 0, 10, 10), None, 0)])
    with pytest.raises(client.AccessibilityError, match=r"The page open in Safari is blocked by the person's permissions \(a site rule\)"):
        desktop.get_state("Safari")  # read, then refused before anything of it is kept
    assert 4 not in desktop._states and desktop.get_state("Safari").app == "Safari"  # the next page: look
    with pytest.raises(client.AccessibilityError, match="The page open in Safari is look only"):
        desktop.focus("Safari", 0)
    assert desktop.get_state("Notes").app == "Notes"  # look: read
    with pytest.raises(client.AccessibilityError, match=r"Notes is look only by the person's permissions: it may be read, not acted in. If the task needs it, ask once: ax.ask\('Notes', 'use', why='what for'\)."):
        desktop.focus("Notes", 0)
    with pytest.raises(client.AccessibilityError, match="Notes is look only"):
        desktop.focus(None, 0)  # the last app, the same way
    with pytest.raises(client.AccessibilityError, match="Messages is blocked by the person's permissions: do without it, or say in your reply that the task needs it."):
        desktop.get_state("Messages")
    assert desktop.get_state("Slack").app == "Slack" and desktop.focus("Slack", 0) == "focused [0] AXTextField ''"  # use: both
    with pytest.raises(client.AccessibilityError, match="1Password is look only"):
        desktop.launch("1Password")  # refused as named, before it starts
    with pytest.raises(client.AccessibilityError, match="1Password is look only"):
        desktop.get_state("1Password")  # resolving would launch it
    assert desktop.ask("Slack", "use", why="reply") == "Slack: already allowed."
    assert desktop.ask("Notes", "use", why="type the list") == "No one to ask: this desktop is not in a session."
    desktop.rules = None  # the scenario page's worker and the observer: no rules, no checks
    assert desktop.get_state("Messages").app == "Messages"


# ----- window capture selection -------------------------------------------------------------------------------


def test_find_window_id_uses_frame_then_title_then_z_order(monkeypatch):
    from app.desktop.ax import capture

    same = (-1920.0, -324.0, 1920.0, 1050.0)
    windows = [
        {"id": 1002, "title": "rebuttal.md — confit_v3", "frame": same, "onscreen": True},
        {"id": 2451, "title": ".env — openai-cua-sample-app", "frame": same, "onscreen": True},
        {"id": 7548, "title": "iclr2027_conference.pdf — wm_rl_iclr2027", "frame": same, "onscreen": True},
        {"id": 5, "title": "", "frame": same, "onscreen": True},
        {"id": 99, "title": "rebuttal.md — confit_v3", "frame": (0.0, 0.0, 400.0, 300.0), "onscreen": False},
    ]
    monkeypatch.setattr(capture, "list_windows", lambda pid: windows)
    monkeypatch.setattr(capture, "onscreen_window_ids", lambda pid: [5, 7548, 2451, 1002])
    # Several windows share the frame: the title decides among them, whatever the z-order.
    assert capture.find_window_id(1, same, "iclr2027_conference.pdf — wm_rl_iclr2027") == 7548
    assert capture.find_window_id(1, same, ".env — openai-cua-sample-app") == 2451
    # No usable title (the window server leaves many titles empty): frontmost among the frame matches.
    assert capture.find_window_id(1, same, "") == 5
    assert capture.find_window_id(1, same, "not a known title") == 5
    # A unique frame match beats a title that only matches elsewhere: frames come from the same element as the title.
    assert capture.find_window_id(1, (0.0, 0.0, 400.0, 300.0), "iclr2027_conference.pdf — wm_rl_iclr2027") == 99
    # No frame at all: title anywhere, frontmost first among equal titles.
    assert capture.find_window_id(1, None, "rebuttal.md — confit_v3") == 1002
    # Nothing matches: the frontmost on-screen window.
    assert capture.find_window_id(1, (5.0, 5.0, 5.0, 5.0), "") == 5
    monkeypatch.setattr(capture, "onscreen_window_ids", lambda pid: [])
    assert capture.find_window_id(1, (5.0, 5.0, 5.0, 5.0), "") == 1002  # first on-screen in list order
    monkeypatch.setattr(capture, "list_windows", lambda pid: [])
    assert capture.find_window_id(1, same, "x") is None


def test_screencapturekit_helpers_open_the_window_server_connection_first(monkeypatch):
    """Regression: building an SCContentFilter before any CoreGraphics call aborts the process (CGS_REQUIRE_INIT)."""
    import types

    from app.desktop.ax import capture

    order = []

    class FakeContent:
        def windows(self):
            return [SimpleNamespace(windowID=lambda: 42)]

        def displays(self):
            return [SimpleNamespace(displayID=lambda: 7)]

    class FakeShareable:
        @staticmethod
        def getShareableContentWithCompletionHandler_(handler):
            handler(FakeContent(), None)

    class FakeConfiguration:
        def __getattr__(self, name):  # every setter is a no-op
            return lambda *args: None

    class FakeFilter:
        @staticmethod
        def alloc():
            return FakeFilter()

        def initWithDesktopIndependentWindow_(self, window):
            order.append("filter")
            return self

        def initWithDisplay_excludingWindows_(self, display, windows):
            order.append("filter")
            return self

    class FakeManager:
        @staticmethod
        def captureImageWithFilter_configuration_completionHandler_(content_filter, configuration, handler):
            handler("cgimage", None)

    fake = types.SimpleNamespace(
        SCShareableContent=FakeShareable, SCContentFilter=FakeFilter, SCScreenshotManager=FakeManager,
        SCStreamConfiguration=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: FakeConfiguration())))
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", fake)
    monkeypatch.setattr(capture, "ensure_core_graphics", lambda: order.append("cg"))
    monkeypatch.setattr(capture, "cgimage_to_pil", lambda image: Image.new("RGB", (4, 3)))
    assert capture.capture_with_screencapturekit(42, (4, 3)).size == (4, 3)
    assert capture.capture_with_screencapturekit(43, (4, 3)) is None  # unknown window: no filter is built
    assert capture.capture_display_with_screencapturekit(7, (0.0, 0.0, 4.0, 3.0), show_cursor=True).size == (4, 3)
    assert order == ["cg", "filter", "cg", "cg", "filter"]


def test_pick_display_uses_center_then_overlap():
    from app.desktop.ax.capture import pick_display

    displays = [(1, (0.0, 0.0, 1512.0, 982.0)), (2, (-1920.0, -354.0, 1920.0, 1080.0)), (3, (-3840.0, -354.0, 1920.0, 1200.0))]
    assert pick_display((-2521.0, -324.0, 600.0, 1170.0), displays)[0] == 3   # center on the third display
    assert pick_display((100.0, 100.0, 400.0, 300.0), displays)[0] == 1
    assert pick_display((-2000.0, -300.0, 1000.0, 500.0), displays)[0] == 2  # center on display 2 (spans two)
    assert pick_display((9000.0, 9000.0, 10.0, 10.0), displays)[0] == 1        # nowhere: most overlap is zero, first wins
    with pytest.raises(RuntimeError):
        pick_display((0.0, 0.0, 1.0, 1.0), [])


def test_observer_source_captures_the_visible_region():
    from app.live.observer.sources import AccessibilitySource

    source = object.__new__(AccessibilitySource)
    source.behind = None
    source.desktop = SimpleNamespace(visible_snapshot=lambda: "visible-image")
    assert source.screenshot() == "visible-image"


def test_the_selected_text_is_read_from_a_field_or_from_the_page():
    from app.desktop.ax.client import SELECTION_CHARS, AccessibilityDesktop, RunningApp

    class SelectionReader(FakeReader):
        def parameterized(self, element, name, parameter):
            assert name == "AXStringForTextMarkerRange"
            return element["page_text"][parameter[0]:parameter[1]]

    page = {"role": "AXWebArea", "AXSelectedTextMarkerRange": (6, 24), "page_text": "Score Decision Index 0.3 public subset"}
    text = {"role": "AXStaticText", "AXParent": page}  # Chromium: the selection is the page's, the focus a text run in it
    field = {"role": "AXTextField", "AXSelectedText": "  report\n draft "}
    desktop = object.__new__(AccessibilityDesktop)
    desktop.reader = SelectionReader()
    on = lambda focused: RunningApp(pid=1, name="Chrome", bundle_id="com.google.Chrome", ref={"AXFocusedUIElement": focused})
    assert desktop.selected_text(on(field)) == "report draft"
    assert desktop.selected_text(on(text)) == "Decision Index 0.3"
    assert desktop.selected_text(on({"role": "AXWebArea"})) == "" and desktop.selected_text(on(None)) == ""
    assert len(desktop.selected_text(on({"role": "AXTextArea", "AXSelectedText": "x" * 900}))) == SELECTION_CHARS


def test_the_text_around_a_selection_or_the_cursor_is_read_from_the_focused_text():
    from app.desktop.ax.client import AccessibilityDesktop, RunningApp

    class TextReader(FakeReader):
        def text_range(self, element, name):  # MacReader unpacks the AXValue; a fake keeps the range itself
            return element.get(name)

    code = "".join(f"line {number}\n" for number in range(1, 21))  # "line 1\n" .. "line 20\n"
    start = code.index("line 10")
    editor = {"role": "AXTextArea", "value": code, "AXNumberOfCharacters": len(code), "AXSelectedTextRange": (start, 7)}
    caret = {**editor, "AXSelectedTextRange": (start, 0)}
    desktop = object.__new__(AccessibilityDesktop)
    desktop.reader = TextReader()
    on = lambda focused: RunningApp(pid=1, name="Code", bundle_id="com.microsoft.VSCode", ref={"AXFocusedUIElement": focused})
    around = desktop.text_around(on(editor))
    assert ">>>line 10<<<" in around  # six lines on each side
    assert "line 4\n" in around and "line 3\n" not in around and "line 16" in around and "line 17" not in around
    assert ">>><<<line 10" in desktop.text_around(on(caret))  # no selection: the cursor, marked
    assert desktop.text_around(on({"role": "AXSecureTextField", **{k: v for k, v in editor.items() if k != "role"}})) == ""
    assert desktop.text_around(on({"role": "AXWebArea"})) == "" and desktop.text_around(on(None)) == ""


def test_a_selection_is_part_of_the_look():
    from app.live.observer.sources import snapshot_hash

    plain = snapshot_hash("Chrome", "page", "tree", "pointer")
    assert snapshot_hash("Chrome", "page", "tree", "pointer", "") == plain  # no selection: the hash it always had
    assert snapshot_hash("Chrome", "page", "tree", "pointer", "Decision Index 0.3") != plain
