"""The tools the live actor offers the model: one ``Tool`` per entry, its Responses API definition next to the
handler that runs it. This is the place to experiment with the agent's capabilities: adding a tool is one more
entry in ``tools_for``. Copied from ``app/responses_loop.py`` (``code_tool_guidance``, ``build_code_tool_definitions``,
``execute_python_tool_call``); the scenario page keeps the originals.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ....processes import assert_active, await_active
from ...config import ACTOR_YIELD_S_DEFAULT
from .cua.client import png_observation

REMOTE_DESKTOP = ("an isolated Ubuntu desktop with its own browser and none of the user's files, apps or accounts; pyautogui, "
                  "log and display only, no ax, and the screenshot is its whole screen.")

# (context, session, call) → the tool's output items for the model. ``call`` holds call_id, the raw ``arguments``
# string, and the parsed ``args``.
Handler = Callable[[Any, Any, dict[str, Any]], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class Tool:
    definition: dict[str, Any]                          # the Responses API function schema sent with every request
    handler: Handler
    validate: Callable[[dict[str, Any]], str | None]    # why the parsed arguments are unacceptable, or None


# ----- exec_py: Python in the desktop worker, the one tool the agent has today -----------------------------------

def exec_py_guidance(where: str, mode: str, yield_s: float) -> tuple[str, list[str]]:
    """``where`` names the desktop the code runs on, e.g. "the user's own local darwin desktop"."""
    if mode == "accessibility":
        return "Execute Python in the persistent accessibility desktop session for this run.", [
            f"Python to execute against apps on {where} through the Accessibility API. Python globals survive between calls.",
            "Available globals: ax, log(value), and display(image), accepting a Pillow image. There is no pyautogui.",
            "Observe: state = ax.get_state('App Name or bundle id or pid'); log(state.text); display(state.screenshot).",
            "state.text is the app's key window as an indexed accessibility tree; state.screenshot shows only that window, and its pixel coordinates equal the (x,y wxh) rects in the tree. state.find('word') filters tree lines.",
            "Act by index: ax.click(app, i); ax.set_value(app, i, 'text'); ax.focus(app, i); ax.perform_action(app, i, 'AXShowMenu'); ax.scroll(app, i, direction='down').",
            "Act on keyboard focus inside the app: ax.type_text(app, 'text'); ax.press_key(app, 'Return' | 'Tab' | 'cmd+a' | 'shift+Tab').",
            "Fallbacks: ax.click(app, x=.., y=..), ax.double_click, ax.right_click, ax.drag(app, (x1, y1), (x2, y2)), ax.launch('App'), ax.list_apps().",
            "Prefer element indexes; use coordinates only when no element fits. Indexes are valid only until the next get_state, so re-read state after acting. Each action returns a short confirmation string worth logging.",
            "The user is working on the same Mac. Nothing you do moves the mouse or changes the frontmost app, so never try to activate windows and never assume keyboard focus outside the target app.",
            "The person's permissions apply to every ax call (the instructions list them); a refused call raises AccessibilityError. log(ax.ask('App', 'use', why='what for')) asks the person for more, waits, and logs their answer: the one way to ask them.",
            cell_note(yield_s),
        ]
    return "Execute Python in the persistent PyAutoGUI desktop session for this run.", [
        f"Python to execute on {where}. Python globals survive between calls.",
        "Available globals: pyautogui, log(value), and display(image), accepting a Pillow image.",
        "Start with display(pyautogui.screenshot()).",
        "Use PyAutoGUI mouse and keyboard input. On macOS (darwin), use command hotkeys; elsewhere use ctrl.",
        "Screenshots use the same coordinates as PyAutoGUI input, including on Retina displays. All coordinates refer to the full desktop screenshot.",
        "Do not change the PyAutoGUI fail-safe setting.",
        cell_note(yield_s),
    ]


def session_env(detail: dict[str, Any]) -> dict[str, str]:
    """What the worker is told of the session it serves: its folder, where the permissions' answers and the agent's asks live."""
    return {"CUA_SESSION_FOLDER": detail["actorSessionFolder"]} if detail.get("actorSessionFolder") else {}


def cell_note(yield_s: float) -> str:
    return (f"A call returns when the code finishes, or after {yield_s:g} seconds with the output so far and a cell id if it "
            "is still running; then wait_cell(cell, timeout_s) returns further output as it happens or the completion, and "
            "stop_cell(cell) interrupts it. Output logged so far is never lost. One cell runs at a time.")


def exec_py_definition(where: str, mode: str, yield_s: float, remote: str | None = None) -> dict[str, Any]:
    """The tool schema; with ``remote`` (what the second desktop is) it also takes ``target``, local or remote."""
    description, guidance = exec_py_guidance(where, mode, yield_s)
    properties: dict[str, Any] = {"code": {"type": "string", "description": "\n".join(guidance)}}
    if remote is not None:
        properties["target"] = {"type": "string", "enum": ["local", "remote"],
                                "description": (f"Where the code runs; each desktop keeps its own Python session and globals. "
                                                f"local: {where}, described above. remote: {remote}")}
    return {
        "type": "function",
        "name": "exec_py",
        "strict": True,
        "description": description,
        "parameters": {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties},
    }


def validate_exec_py(args: dict[str, Any], remote: bool = False) -> str | None:
    if remote and (set(args) != {"code", "target"} or args.get("target") not in ("local", "remote")):
        return "exec_py requires a code string and a target of local or remote."
    if not remote and set(args) != {"code"}:
        return "exec_py requires a code string and no other arguments."
    if not isinstance(args.get("code"), str) or not args["code"].strip() or len(args["code"].encode()) > 64 * 1024:
        return "Python code must be nonempty and at most 64 KiB."
    return None


def yield_s(context: Any) -> float:
    """How long exec_py follows a cell before yielding it to the model: the session setting the run was started
    with (``actorYieldS`` on the run detail, from actor.yield_s or the form)."""
    return float(context.detail.get("actorYieldS") or ACTOR_YIELD_S_DEFAULT)


MAX_WAIT_S = 600.0  # the longest one wait_cell call blocks


async def follow(context: Any, session: Any, cell: Any, budget_s: float) -> list[dict[str, Any]]:
    """Relay a cell's output for up to ``budget_s``: each progress item becomes a run event (and a screenshot when
    it is an image). Returns the items for the model: the new ones plus, if the cell finished, its completion, or
    a note that it is still running."""
    deadline = time.monotonic() + budget_s
    collected: list[dict[str, Any]] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            collected.append({"type": "input_text", "text": f"Still running as cell {cell.id}. Call wait_cell to get more "
                                                            "output or its completion, or stop_cell to interrupt it."})
            return collected
        message = await await_active(cell.next(min(remaining, 5.0)), context.signal)
        assert_active(context.signal)
        if "waiting" in message:
            continue
        if "progress" in message:
            item = message["progress"]
            collected.append(item)
            await relay_item(context, session, item)
            continue
        output = message["output"]
        collected.extend(output[cell.delivered:])  # what the final result adds beyond the progress already relayed
        remote: Any = getattr(session, "remote", None)
        if on_remote(cell, remote):  # the timeline screenshot comes from that desktop
            capture = await await_active(remote.capture(context.signal), context.signal)
            if capture.get("image"):
                await await_active(context.capture_screenshot(session, f"remote-{time.time_ns() // 1_000_000}", png_observation(capture["image"])),
                                   context.signal)
        else:
            await await_active(context.sync_browser_state(session), context.signal)
            await await_active(context.capture_screenshot(session, f"responses-code-turn-{time.time_ns() // 1_000_000}"), context.signal)
        await await_active(context.emit_event({"detail": "exec_py", "level": "ok", "message": "Function tool call completed.",
                                               "type": "function_call_completed"}), context.signal)
        return collected or [{"text": "exec_py completed with no output.", "type": "input_text"}]


async def relay_item(context: Any, session: Any, item: dict[str, Any]) -> None:
    """A progress item as the operator sees it: a run event for text, a screenshot for an image."""
    if item["type"] == "input_image":
        observation = png_observation(item["image_url"][len("data:image/png;base64,"):])
        await await_active(context.capture_screenshot(session, "Code tool image", observation), context.signal)
        detail = "(image)"
    else:
        detail = item.get("text", "")[:2000]
    await await_active(context.emit_event({"detail": detail, "level": "pending", "message": "exec_py output.",
                                           "type": "tool_progress"}), context.signal)


def on_remote(cell: Any, remote: Any) -> bool:
    """Whether a cell runs on the session's remote desktop rather than the local worker."""
    return remote is not None and getattr(cell, "runtime", None) is remote


def running_cell(session: Any) -> Any:
    """The cell in progress on either desktop, if any: one cell runs at a time across both."""
    for runtime in (session.execution, getattr(session, "remote", None)):
        cell = getattr(runtime, "cell", None)
        if cell is not None and not cell.done:
            return cell
    return None


async def run_exec_py(context: Any, session: Any, call: dict[str, Any]) -> list[dict[str, Any]]:
    """Start the code in the worker the target names and follow it for the yield time; then either its result, or a cell id."""
    runtime = session.remote if call["args"].get("target") == "remote" else session.execution
    if runtime is None:
        raise RuntimeError("Python runtime is unavailable. Run uv sync and start a new run.")
    if (cell := running_cell(session)) is not None:
        return [{"type": "input_text", "text": f"Cell {cell.id} is still running; one cell runs at a time. Call wait_cell or stop_cell first."}]
    await await_active(context.emit_event({"detail": f"exec_py {call['arguments']}", "level": "pending",
                                           "message": "Function tool call received from the model.",
                                           "type": "function_call_requested"}), context.signal)
    cell = await await_active(runtime.start(call["args"]["code"], context.signal), context.signal)
    return await follow(context, session, cell, yield_s(context))


async def run_wait_cell(context: Any, session: Any, call: dict[str, Any]) -> list[dict[str, Any]]:
    cell = running_cell(session)
    if cell is None or cell.id != call["args"]["cell"]:
        return [{"type": "input_text", "text": f"No running cell {call['args']['cell']}."}]
    return await follow(context, session, cell, min(float(call["args"]["timeout_s"]), MAX_WAIT_S))


async def run_stop_cell(context: Any, session: Any, call: dict[str, Any]) -> list[dict[str, Any]]:
    cell = running_cell(session)
    if cell is None or cell.id != call["args"]["cell"]:
        return [{"type": "input_text", "text": f"No running cell {call['args']['cell']}."}]
    cell.interrupt()
    return await follow(context, session, cell, 15.0)


def validate_cell_args(args: dict[str, Any], *, with_timeout: bool) -> str | None:
    expected = {"cell", "timeout_s"} if with_timeout else {"cell"}
    if set(args) != expected or not isinstance(args.get("cell"), str):
        return f"{'wait_cell' if with_timeout else 'stop_cell'} takes {', '.join(sorted(expected))} and nothing else."
    if with_timeout and (isinstance(args["timeout_s"], bool) or not isinstance(args["timeout_s"], int | float) or args["timeout_s"] <= 0):
        return "timeout_s must be a positive number of seconds."
    return None


WAIT_CELL = {"type": "function", "name": "wait_cell", "strict": True,
             "description": "Wait on a still-running exec_py cell: returns its further output as it happens, or its completion.",
             "parameters": {"type": "object", "additionalProperties": False, "required": ["cell", "timeout_s"], "properties": {
                 "cell": {"type": "string", "description": "The cell id exec_py returned, e.g. c3."},
                 "timeout_s": {"type": "number", "description": f"How long to wait at most, up to {MAX_WAIT_S:g} seconds."}}}}
STOP_CELL = {"type": "function", "name": "stop_cell", "strict": True,
             "description": "Interrupt a still-running exec_py cell. Its output so far comes back; Python globals survive.",
             "parameters": {"type": "object", "additionalProperties": False, "required": ["cell"], "properties": {
                 "cell": {"type": "string", "description": "The cell id exec_py returned."}}}}


def tools_for(session: Any, context: Any) -> dict[str, Tool]:
    """The tools this run's model may call, keyed by name. Add an entry to give the agent a capability."""
    execution = session.execution
    mode = getattr(execution, "mode", "pyautogui")
    where = getattr(execution, "label", None) or f"the user's own local {execution.platform} desktop"
    remote = getattr(session, "remote", None) is not None  # a second desktop: exec_py takes a target
    return {
        "exec_py": Tool(definition=exec_py_definition(where, mode, yield_s(context), REMOTE_DESKTOP if remote else None),
                        handler=run_exec_py, validate=lambda args: validate_exec_py(args, remote)),
        "wait_cell": Tool(definition=WAIT_CELL, handler=run_wait_cell, validate=lambda args: validate_cell_args(args, with_timeout=True)),
        "stop_cell": Tool(definition=STOP_CELL, handler=run_stop_cell, validate=lambda args: validate_cell_args(args, with_timeout=False)),
    }
