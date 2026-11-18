"""The live actor's desktop worker: a persistent Python REPL on the user's Mac. A copy of app/desktop/worker.py
(which the scenario page keeps); the accessibility client it drives, app/desktop/ax, is shared."""

import base64
import contextlib
import ctypes
import faulthandler
import io
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from PIL import Image

channel = sys.stdout  # the JSON-lines channel to the parent; open_channel() moves it off fd 1


def open_channel():
    """Move the channel onto a private descriptor (non-inheritable, as Python opens them) and point fd 1 at stderr.
    A process a cell starts then inherits stderr as its stdout, so a browser chattering there cannot write into the
    channel, and nothing but the worker holds the channel open once the worker is gone."""
    global channel
    channel = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)


def emit(payload):
    channel.write(json.dumps(payload) + "\n")
    channel.flush()


def check_desktop(pyautogui):
    width, height = pyautogui.size()
    if width <= 0 or height <= 0:
        raise RuntimeError("No local desktop is available. Start the runner in a graphical desktop session.")
    if sys.platform == "darwin":
        services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        if not services.AXIsProcessTrusted():
            raise RuntimeError(
                "macOS Accessibility permission is required for PyAutoGUI. Enable the app launching the runner (Terminal or Codex) in System Settings > Privacy & Security > Accessibility, then retry."
            )
        import Quartz

        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError(
                "macOS Screen Recording permission is required. Enable the app launching the runner in System Settings > Privacy & Security > Screen Recording, then restart it."
            )
    return width, height


def normalize_screenshots(pyautogui):
    # Retina screenshots use physical pixels while input uses logical points.
    # Return images in the same coordinate system as click/moveTo/dragTo.
    capture = pyautogui.screenshot

    def screenshot(imageFilename=None, region=None, **kwargs):
        image = capture(**kwargs)
        size = tuple(pyautogui.size())
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if region is not None:
            x, y, width, height = region
            image = image.crop((x, y, x + width, y + height))
        if imageFilename is not None:
            image.save(imageFilename)
        return image

    pyautogui.screenshot = screenshot


def execute(code, namespace, request_id=None):
    """Run one cell. Every log()/display() item is emitted as a progress line at once (so the runtime can yield
    it) and kept for the final output. SIGINT raises KeyboardInterrupt inside the cell: the partial output
    comes back with the traceback and the globals survive."""
    output: list[dict[str, Any]] = []
    text_bytes = 0
    image_bytes = 0

    def log(*values):
        nonlocal text_bytes
        text = " ".join(str(value) for value in values)
        text_bytes += len(text.encode("utf-8"))
        if text_bytes > 60 * 1024 or len(output) >= 250:
            raise ValueError("Python text output exceeds its size limit.")
        output.append({"type": "input_text", "text": text})
        if request_id is not None:
            emit({"id": request_id, "progress": output[-1]})

    def display(value):
        nonlocal image_bytes
        if isinstance(value, Image.Image):
            buffer = io.BytesIO()
            value.save(buffer, format="PNG")
            value = buffer.getvalue()
        if not isinstance(value, bytes) or not value.startswith(b"\x89PNG\r\n\x1a\n"):
            raise TypeError("display expects a Pillow image or PNG bytes.")
        image_bytes += len(value)
        if image_bytes > 8 * 1024 * 1024 or len(output) >= 250:
            raise ValueError("Python image output exceeds its size limit.")
        output.append(
            {
                "type": "input_image",
                "detail": "original",
                "image_url": "data:image/png;base64," + base64.b64encode(value).decode("ascii"),
            }
        )
        if request_id is not None:
            emit({"id": request_id, "progress": output[-1]})

    class Writer:
        def write(self, text):
            if text.strip():
                log(text)
            return len(text)

        def flush(self):
            pass

    namespace.update(log=log, display=display)
    try:
        with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
            exec(compile(code, "<exec_py>", "exec"), namespace)  # noqa: S102 - Deliberate isolated model-code worker.
    except BaseException as error:  # noqa: BLE001 - Report arbitrary model errors and retain the REPL.
        failsafe = getattr(namespace.get("pyautogui"), "FailSafeException", ())
        if isinstance(error, failsafe):
            return {"error": {"code": "python_failsafe", "message": "Desktop fail-safe activated."}}
        # Preserve partial output and globals so the model can correct its code.
        output.append({"type": "input_text", "text": traceback.format_exc()[-4000:]})
    return {"output": output or [{"type": "input_text", "text": "exec_py completed with no output."}]}


def serve_requests(namespace, stream):
    terminal_error = None
    for line in stream:
        if len(line.encode("utf-8")) > 512 * 1024:
            raise ValueError("Python request exceeds its size limit.")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise TypeError("Python request must be an object.")
        operation = request.get("operation")
        if terminal_error is not None:
            emit({"id": request.get("id"), "error": terminal_error})
            continue
        if operation == "capture":
            try:
                capture = capture_snapshot(namespace)
            except Exception as error:  # noqa: BLE001 - a failed timeline screenshot must not end the run
                capture = {"image": None, "error": f"{type(error).__name__}: {error}"[:500]}
            emit({"id": request.get("id"), "capture": capture})
            continue
        if operation != "execute":
            emit(
                {
                    "id": request.get("id"),
                    "error": {
                        "code": "unsupported_python_operation",
                        "message": "Only the execute operation is supported.",
                    },
                }
            )
            continue
        code = request.get("code")
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 64 * 1024:
            raise ValueError("Python code must be nonempty and at most 64 KiB.")
        result = execute(code, namespace, request.get("id"))
        if (rules := getattr(namespace.get("ax"), "rules", None)) is not None:
            rules.cell_done()  # an Allow once served this cell; the next asks again
        terminal_error = result.get("error")
        emit({"id": request.get("id"), **result})


def check_screenshot(pyautogui, expected_size):
    image = pyautogui.screenshot()
    if tuple(pyautogui.size()) != expected_size or image.size != expected_size:
        raise RuntimeError("Desktop screenshot dimensions changed. Start a new run.")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if len(buffer.getvalue()) > 8 * 1024 * 1024:
        raise RuntimeError("Desktop screenshot exceeds 8 MiB.")


def accessibility_package():
    # worker.py runs by path. Prefer the installed app package; otherwise import it from beside this file.
    try:
        from app.desktop import ax
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # python-app, four levels up from live/actor/desktop
        from app.desktop import ax
    return ax


def resolve_mode():
    """CUA_DESKTOP_MODE selects real input (pyautogui, default) or the macOS Accessibility API (accessibility)."""
    value = (os.environ.get("CUA_DESKTOP_MODE") or "pyautogui").strip().lower()
    aliases = {"pyautogui": "pyautogui", "accessibility": "accessibility", "ax": "accessibility", "a11y": "accessibility"}
    if value not in aliases:
        raise RuntimeError(f"CUA_DESKTOP_MODE must be 'pyautogui' or 'accessibility', not {value!r}.")
    if aliases[value] == "accessibility" and sys.platform != "darwin":
        raise RuntimeError("CUA_DESKTOP_MODE=accessibility is only available on macOS.")
    return aliases[value]


def session_rules():
    """The person's permissions, when this worker runs for a live session (CUA_SESSION_FOLDER names its folder); none elsewhere."""
    folder = os.environ.get("CUA_SESSION_FOLDER")
    if not folder:
        return None
    from app.live.config import agent_rules  # importable once accessibility_package() has found the package

    return agent_rules(Path(folder))


def build_namespace(mode):
    """The globals model code sees. Accessibility mode never loads PyAutoGUI, so the real mouse is never touched."""
    if mode == "accessibility":
        ax = accessibility_package()
        ax.AccessibilityDesktop.check_permissions()
        return {"__builtins__": __builtins__, "ax": ax.AccessibilityDesktop(rules=session_rules())}
    import pyautogui

    width, height = check_desktop(pyautogui)
    normalize_screenshots(pyautogui)
    check_screenshot(pyautogui, (width, height))
    return {"__builtins__": __builtins__, "pyautogui": pyautogui}


def capture_snapshot(namespace):
    """Timeline screenshot for the runner: the observed app window (accessibility) or the whole desktop (PyAutoGUI)."""
    desktop = namespace.get("ax")
    image = None
    app = title = ""
    if desktop is not None:
        image = desktop.snapshot()
        state = desktop.last_state()
        if state is not None:
            app, title = state.app, state.window_title
    elif namespace.get("pyautogui") is not None:
        image = namespace["pyautogui"].screenshot()
    if image is None:
        return {"image": None}
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"image": base64.b64encode(buffer.getvalue()).decode("ascii"), "app": app, "title": title}


def main():
    # A native crash (an assertion in a macOS framework) then reports the Python stack on stderr, which the
    # runner attaches to its "worker exited" error instead of losing it.
    open_channel()
    faulthandler.enable()
    signal.signal(signal.SIGINT, signal.default_int_handler)  # the runtime interrupts a cell with SIGINT
    mode = resolve_mode()
    namespace = build_namespace(mode)
    emit({"ready": True, "platform": sys.platform, "mode": mode})
    if "--check" in sys.argv:
        return
    if os.name != "nt":
        parent = os.getppid()

        def watch_parent():
            while True:
                time.sleep(1)
                if os.getppid() != parent:
                    os.killpg(os.getpgrp(), signal.SIGKILL)

        threading.Thread(target=watch_parent, daemon=True).start()
    serve_requests(namespace, sys.stdin)


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:  # noqa: BLE001 - The parent requires a protocol error for any startup failure.
        emit({"error": f"{type(error).__name__}: {error}"[:4000]})
        sys.exit(1)
