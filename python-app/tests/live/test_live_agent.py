"""The live actor's agent loop, its tools (exec_py yielding into wait_cell / stop_cell), and its instructions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import RunnerCoreError
from app.live.actor.desktop.agent_loop import classify_response, run_agent_loop
from app.live.actor.desktop.tools import tools_for
from app.live.actor.prompts import TASK_RULES, ActorInput, instructions, live_context_text
from app.live.storage.records import as_summaries

OK = {"type": "input_text", "text": "ok"}


def call(code="log('hello')", call_id="call-1", name="exec_py", arguments=None):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments or {"code": code})}


def message(text="Done.", phase=None):
    return {"type": "message", "role": "assistant", "phase": phase, "content": [{"type": "output_text", "text": text}]}


def response(*output, response_id="response-1"):
    return {"id": response_id, "status": "completed", "output": list(output)}


class FakeCell:
    """A scripted cell: ``events`` are ("progress", item), ("sleep", seconds) for time passing, or ("output", items);
    after them it reports waiting."""

    def __init__(self, cell_id, events):
        self.id, self.events, self.done, self.delivered, self.output, self.interrupted = cell_id, list(events), False, 0, None, False

    async def next(self, timeout_s):
        if self.done:
            return {"output": self.output}
        if self.interrupted:  # an interrupt finishes the cell with a traceback after what it logged
            self.output = [{"type": "input_text", "text": f"logged {self.delivered}"}] * self.delivered + [{"type": "input_text", "text": "KeyboardInterrupt"}]
            self.done = True
            return {"output": self.output}
        if not self.events:
            await asyncio.sleep(0.01)
            return {"waiting": True}
        kind, payload = self.events.pop(0)
        if kind == "sleep":
            await asyncio.sleep(payload)
            return {"waiting": True}
        if kind == "progress":
            self.delivered += 1
            return {"progress": payload}
        self.output, self.done = payload, True
        return {"output": payload}

    def interrupt(self):
        self.interrupted = True


class FakeRuntime:
    def __init__(self, scripts):
        self.scripts, self.started, self.cell, self.platform, self.mode = scripts, [], None, "darwin", "accessibility"

    async def start(self, code, signal=None):
        self.started.append(code)
        self.cell = FakeCell(f"c{len(self.started)}", self.scripts.get(code, [("output", [OK])]))
        return self.cell


def harness(*responses, scripts=None):
    context = SimpleNamespace(signal=asyncio.Event(), detail={"run": {"prompt": "Total the sheet.", "model": "test-model"}, "actorYieldS": 0.05},
                              emit_event=AsyncMock(), sync_browser_state=AsyncMock(), capture_screenshot=AsyncMock())
    session = SimpleNamespace(execution=FakeRuntime(scripts or {}))
    client = SimpleNamespace(create=AsyncMock(side_effect=responses))
    return context, session, client


def events(context):
    return [entry.args[0]["type"] for entry in context.emit_event.call_args_list]


async def test_loop_runs_tools_from_the_registry_and_returns_the_final_message():
    context, session, client = harness(
        response(message("Looking.", "commentary"), call(), response_id="r1"),
        response(call("log('second')", "call-2"), response_id="r2"),
        response(message(), response_id="r3"))
    result = await run_agent_loop(context, session, "Instructions", 3, client=client)
    assert result["final_assistant_message"] == "Done."
    requests = [entry.args[0] for entry in client.create.call_args_list]
    assert requests[0]["input"] == "Total the sheet." and "previous_response_id" not in requests[0]
    assert requests[1]["previous_response_id"] == "r1" and requests[2]["previous_response_id"] == "r2"
    assert requests[1]["input"] == [{"type": "function_call_output", "call_id": "call-1", "output": [OK]}]
    assert [tool["name"] for tool in requests[0]["tools"]] == ["exec_py", "wait_cell", "stop_cell"]
    guidance = requests[0]["tools"][0]["parameters"]["properties"]["code"]["description"]
    assert "Accessibility API" in guidance and "after 0.05 seconds" in guidance  # the run's own yield time
    assert session.execution.started == ["log('hello')", "log('second')"]
    kinds = events(context)
    assert kinds.count("function_call_requested") == 2 and kinds.count("function_call_completed") == 2 and kinds[-1] == "run_progress"


async def test_long_cell_yields_then_wait_cell_returns_the_rest():
    slide1, slide2 = {"type": "input_text", "text": "slide 1 ready"}, {"type": "input_text", "text": "slide 2 ready"}
    context, session, client = harness(
        response(call("loop"), response_id="r1"),
        response(call(name="wait_cell", call_id="call-2", arguments={"cell": "c1", "timeout_s": 5}), response_id="r2"),
        response(message("Walked through the slides."), response_id="r3"),
        scripts={"loop": [("progress", slide1), ("sleep", 0.2),  # the yield time passes here
                          ("progress", slide2), ("output", [slide1, slide2, {"type": "input_text", "text": "done"}])]})
    result = await run_agent_loop(context, session, "Instructions", 3, client=client)
    assert result["final_assistant_message"] == "Walked through the slides."
    requests = [entry.args[0] for entry in client.create.call_args_list]
    first = requests[1]["input"][0]["output"]
    assert first[0] == slide1 and "Still running as cell c1" in first[1]["text"]  # yielded with the output so far
    # wait_cell returns the new item and what the completion added, never slide 1 again
    second = requests[2]["input"][0]["output"]
    assert [item["text"] for item in second] == ["slide 2 ready", "done"]
    kinds = events(context)
    assert kinds.count("tool_progress") == 2 and kinds.count("function_call_completed") == 1


async def test_stop_cell_interrupts_and_one_cell_runs_at_a_time():
    context, session, client = harness(
        response(call("loop"), response_id="r1"),
        response(call("log('meanwhile')", "call-2"), response_id="r2"),
        response(call(name="stop_cell", call_id="call-3", arguments={"cell": "c1"}), response_id="r3"),
        response(message(), response_id="r4"),
        scripts={"loop": [("progress", {"type": "input_text", "text": "tick"})]})
    await run_agent_loop(context, session, "Instructions", 4, client=client)
    requests = [entry.args[0] for entry in client.create.call_args_list]
    assert "one cell runs at a time" in requests[2]["input"][0]["output"][0]["text"]  # the second exec_py was refused
    stopped = requests[3]["input"][0]["output"]
    assert stopped[-1]["text"] == "KeyboardInterrupt" and session.execution.cell.interrupted
    assert session.execution.started == ["loop"]  # the refused exec_py never started a cell


async def test_a_cell_still_running_at_the_final_message_is_stopped():
    context, session, client = harness(response(call("loop"), response_id="r1"), response(message(), response_id="r2"),
                                       scripts={"loop": [("progress", {"type": "input_text", "text": "tick"})]})
    await run_agent_loop(context, session, "Instructions", 2, client=client)
    assert session.execution.cell.interrupted and session.execution.cell.done


async def test_unknown_tool_and_bad_arguments_are_rejected_before_anything_runs():
    context, session, client = harness(response(call(name="open_url"), message()))
    with pytest.raises(RunnerCoreError, match="Unexpected function call: open_url"):
        await run_agent_loop(context, session, "Instructions", 2, client=client)
    assert session.execution.started == []
    tools = tools_for(session, context)
    with pytest.raises(RunnerCoreError, match="nonempty"):
        classify_response(response(call(code="   ")), tools)
    with pytest.raises(RunnerCoreError, match="no other arguments"):
        classify_response(response(call(arguments={"code": "x", "extra": 1})), tools)
    with pytest.raises(RunnerCoreError, match="positive number"):
        classify_response(response(call(name="wait_cell", arguments={"cell": "c1", "timeout_s": 0})), tools)
    with pytest.raises(RunnerCoreError, match="stop_cell takes cell"):
        classify_response(response(call(name="stop_cell", arguments={"cell": "c1", "timeout_s": 3})), tools)


async def test_turn_exhaustion_is_not_success():
    context, session, client = harness(response(call()), response(call("log(2)", "call-2")))
    with pytest.raises(RuntimeError, match="2-turn budget"):
        await run_agent_loop(context, session, "Instructions", 2, client=client)


def test_instructions_follow_the_worker_mode_and_end_with_the_task_rules():
    given = {"worklog": as_summaries([{"windowStart": "2026-09-17T10:00:00Z", "windowEnd": "2026-09-17T10:05:00Z", "summary": "Wrote a memo."}]),
             "proposal": None}
    tail = f"{TASK_RULES[-1]}\n\n{live_context_text(given)}"  # the live context comes last

    def build(agent, mode="", desktops=("local",)):
        return instructions(ActorInput(agent=agent, task="Tidy the notes", input=given, mode=mode, desktops=desktops))

    ax = build("desktop", "accessibility")
    assert "Accessibility API" in ax and "ax.get_state" in ax and ax.endswith(tail) and "Wrote a memo." in ax
    gui = build("desktop", "pyautogui")
    assert "PyAutoGUI" in gui and "subprocess.run(['open', '-a', name])" in gui and gui.endswith(tail)
    cmux = build("cmux")  # the cmux agent: its own block, none of the desktop agent's, the same task rules
    assert "cmux Computer Use" in cmux and "exec_py" not in cmux and cmux.endswith(tail)


async def test_exec_py_takes_a_target_only_when_a_remote_desktop_is_connected():
    local, remote = FakeRuntime({}), FakeRuntime({})
    context = SimpleNamespace(detail={"actorYieldS": 0.05}, signal=asyncio.Event(), emit_event=AsyncMock(), capture_screenshot=AsyncMock(),
                              sync_browser_state=AsyncMock())
    tools = tools_for(SimpleNamespace(execution=local, remote=remote), context)
    schema = tools["exec_py"].definition["parameters"]
    assert schema["required"] == ["code", "target"] and schema["properties"]["target"]["enum"] == ["local", "remote"]
    assert "Ubuntu" in schema["properties"]["target"]["description"]
    assert tools["exec_py"].validate({"code": "x"}) and tools["exec_py"].validate({"code": "x", "target": "moon"})
    assert tools["exec_py"].validate({"code": "x", "target": "remote"}) is None
    session = SimpleNamespace(execution=local, remote=remote)
    await tools["exec_py"].handler(context, session, {"arguments": '{"target": "remote"}', "args": {"code": "log('r')", "target": "remote"}})
    await tools["exec_py"].handler(context, session, {"arguments": '{"target": "local"}', "args": {"code": "log('l')", "target": "local"}})
    assert remote.started == ["log('r')"] and local.started == ["log('l')"]
    plain = tools_for(SimpleNamespace(execution=local, remote=None), context)  # no remote: the tool is what it was
    assert "target" not in plain["exec_py"].definition["parameters"]["properties"]
    assert plain["exec_py"].validate({"code": "x", "target": "local"}) and plain["exec_py"].validate({"code": "x"}) is None
    def build(desktops):
        return instructions(ActorInput(agent="desktop", task="t", input={"worklog": [], "proposal": None}, mode="pyautogui", desktops=desktops))

    both = build(("local", "remote"))  # a block per desktop present
    assert "user's Mac" in both and "isolated Ubuntu desktop" in both and "Ubuntu" not in build(("local",))
    alone = build(("remote",))
    assert "isolated Ubuntu desktop" in alone and "Mac" not in alone
