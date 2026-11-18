"""The claude agent: Claude Code through the Agent SDK, its messages relayed as run events, its permission requests answered
from the console, and the desktop tool server it is given."""

import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import httpx
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from live_helpers import FakeClient, fake_settings
from PIL import Image

from app.api import create_app
from app.errors import RunnerCoreError
from app.live import health
from app.live.actor.prompts import STANDALONE_INTROS, ActorInput, instructions, live_context_text
from app.live.actor.standalone import approvals, browser, claude, desktop_server
from app.live.actor.standalone.servers import PLAYWRIGHT_MCP
from app.live.health import check_settings, probe_claude
from app.live.storage.records import as_summaries
from app.runner import RunnerManager


def jpeg(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


USAGE = {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 7}


def assistant(message_id: str, block, **usage: int) -> AssistantMessage:
    return AssistantMessage(content=[block], model="claude-sonnet-5-5", usage=usage, message_id=message_id)


def result(**changes) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="sess-1", total_cost_usd=0.05, result="Done.",
                         model_usage={"claude-sonnet-5-5": {"inputTokens": 4, "outputTokens": 30, "cacheReadInputTokens": 200, "cacheCreationInputTokens": 50, "costUSD": 0.05}},
                         **changes)


INIT = SystemMessage(subtype="init", data={"session_id": "sess-1", "model": "claude-sonnet-5-5", "permissionMode": "auto",
                                           "tools": ["Bash", "mcp__desktop__exec_py", "mcp__browser__browser_navigate"],
                                           "mcp_servers": [{"name": "desktop", "status": "connected"}, {"name": "browser", "status": "connected"}]})
SHOT = UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content=[
    {"type": "text", "text": "window tree"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg(4, 3)}}])])
MESSAGES = [
    INIT,
    assistant("msg_1", TextBlock(text="Looking at the window."), **USAGE),
    assistant("msg_1", ToolUseBlock(id="toolu_1", name="mcp__desktop__exec_py", input={"code": "display(ax.get_state('Notes').screenshot)"}), **USAGE),
    ("frame",),  # the desktop server leaves a frame of the Mac after the step
    SHOT,
    assistant("msg_2", TextBlock(text="Done."), **USAGE),
    result(),
]


class FakeSDK:
    """Stands in for ClaudeSDKClient: keeps the options it was given and the prompt it was asked, plays a script (a
    message, or ("ask", tool, input) which puts the question to the permission callback as the SDK would, ("frame",) which
    leaves a frame of the Mac where the desktop server would, ("hang",), ("fail", stderr) or ("missing",)), and notes when it
    was disconnected."""

    script: ClassVar[list] = []
    instances: ClassVar[list] = []

    def __init__(self, options):
        self.options = options
        self.prompts: list[str] = []
        self.answers: list = []
        self.disconnected = False
        FakeSDK.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.disconnected = True

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        for step in FakeSDK.script:
            if isinstance(step, tuple) and step[0] == "ask":
                self.answers.append(await self.options.can_use_tool(step[1], step[2], ToolPermissionContext(tool_use_id="toolu_ask")))
            elif isinstance(step, tuple) and step[0] == "frame":
                folder = Path(self.options.mcp_servers["desktop"]["env"]["CUA_DESKTOP_FRAMES"])
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "1.png").write_bytes(base64.b64decode(png(5, 1)))
            elif isinstance(step, tuple) and step[0] == "hang":
                await asyncio.sleep(60)
            elif isinstance(step, tuple) and step[0] == "fail":
                raise ProcessError("Command failed with exit code 1", exit_code=1, stderr=step[1])
            elif isinstance(step, tuple) and step[0] == "missing":
                raise CLINotFoundError("Claude Code not found")
            else:
                await asyncio.sleep(0)
                yield step


ENDPOINT = "http://127.0.0.1:1"
SEEN: list[str] = []  # every browser tool result the agent browser was told of


@pytest.fixture
def sdk(monkeypatch):
    """The SDK's client replaced by the fake, the session's agent browser by a stub."""
    monkeypatch.setattr(claude, "ClaudeSDKClient", FakeSDK)
    async def frame():  # the tab after a browser step, as the run files it
        return {"base64": png(2, 1), "width": 2, "height": 1}

    monkeypatch.setitem(browser.OPEN, ENDPOINT, SimpleNamespace(seen=lambda text: SEEN.append(text), frame=frame))
    FakeSDK.script, FakeSDK.instances = list(MESSAGES), []
    SEEN.clear()
    return FakeSDK


FAKE_CLAUDE = """#!/bin/sh
echo "2.1.293 (Claude Code)"
"""
FAKE_NPX = "#!/bin/sh\necho 10.9.2\n"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A ``claude`` and an ``npx`` on PATH that answer --version, for the configuration check."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "claude").write_text(FAKE_CLAUDE)
    (binaries / "npx").write_text(FAKE_NPX)
    for name in ("claude", "npx"):
        (binaries / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    return binaries


def make_context(tmp_path, **detail):
    events: list[dict] = []
    shots: list[tuple] = []

    async def emit_event(event):
        events.append(event)

    async def capture_screenshot(session, label, observation):
        shots.append((session.target_label, label, observation["width"], observation["height"]))

    run_dir = tmp_path / "run"
    (run_dir / "workspace").mkdir(parents=True)
    context = SimpleNamespace(
        detail={"run": {"id": "run-1", "prompt": "Tidy the notes", "model": "claude-default", "maxResponseTurns": 6, "startedAt": "2026-10-19T15:00:00Z"},
                "workspacePath": str(run_dir / "workspace"), "liveContext": "Observed context: notes.md is open", "actorAgent": "claude",
                "actorBrowserEndpoint": ENDPOINT, "actorDesktopSource": ["local"], **detail},
        signal=asyncio.Event(), screenshot_directory=run_dir / "screenshots", emit_event=emit_event, capture_screenshot=capture_screenshot,
    )
    return context, events, shots


NOTES = {"worklog": as_summaries([{"windowStart": "2026-10-02T15:00:00Z", "windowEnd": "2026-10-02T15:01:00Z", "summary": "notes.md is open"}]),
         "proposal": None}


class FakeLive:
    """A session that set up every agent."""

    def __init__(self, folder, remote=None):
        self.folder = folder
        self.remote = remote  # (provider, url, token) when the session has the remote desktop too
        self.finished: list[tuple[str, str]] = []

    def actor_inputs(self):
        return NOTES

    def run_directory(self, run_id):
        return self.folder / run_id

    def session_folder(self):
        return self.folder.parent

    def require_running(self):
        pass  # never paused

    def actor_agents(self):
        return ("desktop", "cmux", "claude", "codex")

    def claude_settings(self):
        return {"actorClaudeModel": "sonnet", "actorClaudePermissionMode": "auto"}

    def codex_settings(self):
        return {"actorCodexSandbox": "workspace-write"}

    async def agent_browser_endpoint(self):
        return ENDPOINT

    def desktop_source(self):
        return ("local", "remote") if self.remote else ("local",)

    def desktop_remote(self):
        return self.remote

    def record_run(self, run_id, context, prompt="", chat=None, proposal=None):
        pass

    def record_run_finished(self, run_id, status, chat=None):
        self.finished.append((run_id, status))


async def test_a_claude_run_is_started_followed_and_stored(tmp_path, sdk):
    live = FakeLive(tmp_path / "data" / "sessions" / "20261002-150000" / "runs")
    manager = RunnerManager(tmp_path / "data", live=live)
    started = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "Tidy the notes", "maxResponseTurns": 6,
                                       "liveContext": True, "agent": "claude"})
    run_id = started["run"]["id"]
    assert started["actorAgent"] == "claude" and started["actorClaudeModel"] == "sonnet" and "actorYieldS" not in started
    assert started["actorBrowserEndpoint"] == ENDPOINT and started["actorDesktopSource"] == ["local"]
    await manager.contexts[run_id].execution
    detail = await manager.get_run_detail(run_id)
    assert detail["run"]["status"] == "completed" and detail["run"]["model"] == "claude-sonnet-5-5" and live.finished == [(run_id, "completed")]
    assert detail["run"]["usage"]["costUsd"] == 0.05 and detail["run"]["summary"]["notes"] == ["Executed the task with Claude Code.", "Model final response: Done."]
    messages = [event["message"] for event in detail["events"]]
    order = ["Claude Code started.", "Claude Code session started.", "Claude turn 1 completed.", "Model returned a final response."]
    assert [messages.index(m) for m in order] == sorted(messages.index(m) for m in order)
    assert "Function tool call received from the model." in messages and "mcp__desktop__exec_py output." in messages
    assert [shot["imageWidth"] for shot in detail["browser"]["screenshots"]] == [4, 5]  # exec_py's image, then the frame of the Mac the desktop server left
    # Claude Code ran through the SDK in the run's workspace, with the agent's instructions on its system prompt, the
    # run's two MCP servers and only those (allowed without asking), the person as its permission prompt, and the
    # session's model and permission mode; the prompt was the one message.
    [client] = sdk.instances
    opts = client.options
    folder = live.folder / run_id / "claude"
    inputs = ActorInput(agent="claude", task="Tidy the notes", input=NOTES, permissions="apps not listed: look")  # told the person's rules, read from the session
    assert client.prompts == ["Tidy the notes"] and client.disconnected
    assert opts.cwd == detail["workspacePath"] and opts.cli_path.endswith("claude")
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": instructions(inputs)} and opts.setting_sources == ["user", "project", "local"]
    assert opts.mcp_servers["desktop"] == {"command": sys.executable, "args": ["-m", "app.live.actor.standalone.desktop_server"],
                                           "env": {"CUA_ACTOR_DESKTOP_SOURCE": "local", "CUA_DESKTOP_FRAMES": str(folder / "frames"), "CUA_SESSION_FOLDER": str(live.folder.parent)}}
    assert opts.mcp_servers["browser"]["args"] == ["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", ENDPOINT, "--output-dir", str(folder / "browser")]
    assert opts.mcp_servers["history"]["args"] == ["-m", "app.live.actor.standalone.history_server"] and "CUA_DATA_ROOT" in opts.mcp_servers["history"]["env"] and opts.mcp_servers["history"]["env"]["CUA_HISTORY_BEFORE"] == detail["run"]["startedAt"]
    assert not opts.strict_mcp_config and opts.allowed_tools == ["mcp__desktop", "mcp__browser", "mcp__history"] and opts.can_use_tool is not None  # the login's connectors come in too
    assert (opts.model, opts.permission_mode, opts.max_turns, opts.resume) == ("sonnet", "auto", None, None)  # no turn cap: Claude stops when done, or when the person stops it
    assert opts.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": ""}  # the backend's Anthropic key set never reaches Claude Code: it runs on the login
    assert (folder / "instructions.md").read_text() == f"{instructions(inputs)}\n"
    assert instructions(inputs).startswith(STANDALONE_INTROS["claude"]) and "target" not in instructions(inputs) and instructions(inputs).endswith("notes.md is open")
    assert ("Where to work: the user usually wants the task done both efficiently and effectively. Tools that need no screen (e.g., bash, web_search, etc.) "
            "are usually faster than interacting with a screen, so prefer them when they can do the step; when using a screen is the faster way/necessary, "
            "consider this order: 1. a task that needs only web pages: the browser tools; 2. the user's Mac only when" in instructions(inputs)
            and "remote desktop" not in instructions(inputs))  # without a remote desktop, that rule is left out
    assert (live.folder / run_id / "live_context.md").read_text() == live_context_text(NOTES) == detail["liveContext"]


async def test_with_the_remote_desktop_too_the_desktop_server_is_told_where_it_is_but_not_its_token(tmp_path, sdk):
    live = FakeLive(tmp_path / "data" / "sessions" / "20261002-150000" / "runs", remote=("cua", "wss://desk.example", "secret-token"))
    manager = RunnerManager(tmp_path / "data", live=live)
    started = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "Tidy the notes", "maxResponseTurns": 6, "liveContext": True, "agent": "claude"})
    assert started["actorDesktopSource"] == ["local", "remote"] and started["desktopRemoteProvider"] == "cua" and "secret-token" not in json.dumps(started)
    await manager.contexts[started["run"]["id"]].execution
    [client] = sdk.instances
    assert client.options.mcp_servers["desktop"]["env"] == {"CUA_ACTOR_DESKTOP_SOURCE": "local,remote", "CUA_DESKTOP_REMOTE_PROVIDER": "cua", "CUA_DESKTOP_REMOTE_URL": "wss://desk.example",
                                                            "CUA_DESKTOP_FRAMES": str(live.folder / started["run"]["id"] / "claude" / "frames"),
                                                            "CUA_SESSION_FOLDER": str(live.folder.parent)}  # its worker reads the permissions and writes the asks there
    written = client.options.system_prompt["append"]
    assert "exec_py also takes a target: remote is an isolated Ubuntu desktop" in written
    assert "1. a task that needs only web pages: the browser tools; 2. a task that needs other apps beyond browser: the remote desktop, whenever it can be done there; 3. the user's Mac only when" in written


async def test_a_permission_request_waits_for_the_console_and_is_answered(tmp_path, sdk):
    sdk.script = [INIT, assistant("msg_1", ToolUseBlock(id="toolu_1", name="Bash", input={"command": "ls"}), **USAGE), ("ask", "Bash", {"command": "ls"}), *MESSAGES[3:]]
    context, events, _ = make_context(tmp_path)
    task = asyncio.create_task(claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=None)))
    while not any(event["message"] == "Claude is waiting for your approval to use Bash." for event in events):
        await asyncio.sleep(0.02)
    assert "run-1" in approvals.WAITING and events[-1]["detail"].startswith('Bash {"command": "ls"}')
    with pytest.raises(RunnerCoreError, match="mode must be one of"):
        await approvals.answer("run-1", "bypass")
    await approvals.answer("run-1", "once")
    outcome = await task
    assert outcome["final_assistant_message"] == "Done." and "run-1" not in approvals.WAITING
    [client] = sdk.instances
    assert client.answers == [PermissionResultAllow(updated_input={"command": "ls"})]  # what Claude got back
    assert "Approval answered: allow." in [event["message"] for event in events]
    with pytest.raises(RunnerCoreError) as refused:
        await approvals.answer("run-1", "deny")
    assert refused.value.code == "approval_not_pending"


async def test_a_denied_request_tells_claude_so(tmp_path, sdk):
    sdk.script = [INIT, ("ask", "Bash", {"command": "rm -rf notes"}), *MESSAGES[5:]]
    context, events, _ = make_context(tmp_path)
    task = asyncio.create_task(claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=None)))
    while "run-1" not in approvals.WAITING or not approvals.WAITING["run-1"].queue:
        await asyncio.sleep(0.02)
    await approvals.answer("run-1", "deny")
    await task
    [client] = sdk.instances
    assert client.answers == [PermissionResultDeny(message="The user denied this.")]
    assert "Approval answered: deny." in [event["message"] for event in events]


async def test_the_console_answers_a_claude_run_through_the_same_route_as_a_cmux_run(tmp_path, monkeypatch):
    from live_helpers import make_manager
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    waiting = SimpleNamespace(queue=[("Bash", {"command": "ls"}, None)], answer=AsyncMock())
    monkeypatch.setitem(approvals.WAITING, "run-9", waiting)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        answered = await http.post("/api/live/session/runs/run-9/approval", json={"mode": "deny"})
        assert answered.status_code == 200 and answered.json() == {"runId": "run-9", "mode": "deny"}
        waiting.answer.assert_awaited_once_with("deny")
        assert (await http.post("/api/live/session/runs/other-run/approval", json={"mode": "once"})).json()["code"] == "approval_not_pending"  # not a claude run: asked of cmux, which has nothing waiting


async def test_a_resumed_turn_counts_only_its_own_usage(tmp_path, sdk):
    sdk.script = MESSAGES
    before = {"claude-sonnet-5-5": {"inputTokens": 1, "outputTokens": 10, "cacheReadInputTokens": 50, "cacheCreationInputTokens": 20, "costUSD": 0.02}}
    context, _, _ = make_context(tmp_path, chat={"turn": 2, "resume": "sess-0", "resumeTotals": before})
    await claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=None))
    usage = context.detail["run"]["usage"]
    assert (usage["costUsd"], usage["outputTokens"], usage["inputTokens"]) == (0.03, 20, 3 + 150 + 30)  # the session's totals less the previous turn's
    assert context.detail["chat"]["claudeTotals"] == result().model_usage  # whole, for the next turn to take off


async def test_stopping_a_claude_run_ends_claude(tmp_path, sdk):
    sdk.script = [*MESSAGES[:2], ("hang",)]  # Claude keeps working after its first message
    context, events, _ = make_context(tmp_path, actorClaudeCwd=str(tmp_path), chat={"turn": 2, "resume": "sess-0"})
    task = asyncio.create_task(claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=None)))
    while not any(event["message"] == "Claude turn 1 completed." for event in events):
        await asyncio.sleep(0.02)
    context.signal.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.detail["chat"] == {"turn": 2, "resume": "sess-0", "claudeSessionId": "sess-1"}  # a stopped turn can still be resumed
    [client] = sdk.instances
    assert client.options.resume == "sess-0" and client.options.cwd == str(tmp_path) and client.disconnected  # disconnecting ends Claude


async def test_a_claude_that_fails_to_start_fails_the_run_with_its_error(tmp_path, sdk):
    sdk.script = [("fail", "Not logged in")]
    context, _, _ = make_context(tmp_path)
    with pytest.raises(RuntimeError, match="exited with code 1 and no result. Not logged in"):
        await claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=NOTES))


async def test_a_missing_claude_is_said_so(tmp_path, sdk):
    sdk.script = [("missing",)]
    context, _, _ = make_context(tmp_path)
    with pytest.raises(RunnerCoreError) as missing:
        await claude.run_claude_agent(context, ActorInput(agent="claude", task="Tidy the notes", input=NOTES))
    assert missing.value.code == "agent_unavailable"


async def test_a_browser_tool_result_tells_the_agent_browser_where_the_agent_is(tmp_path, sdk):
    sdk.script = [INIT, assistant("msg_1", ToolUseBlock(id="toolu_1", name="mcp__browser__browser_navigate", input={"url": "https://example.com"}), **USAGE),
                  UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content=[{"type": "text", "text": "### Page\n- Page URL: https://example.com/\n- Page Title: Example"}])]),
                  *MESSAGES[5:]]
    context, _, shots = make_context(tmp_path)
    await claude.run_claude_agent(context, ActorInput(agent="claude", task="Open it", input=None))
    assert SEEN == ["### Page\n- Page URL: https://example.com/\n- Page Title: Example"]
    assert shots == [("Claude Code", "mcp__browser__browser_navigate", 2, 1)]  # the frame after the step is the run's screenshot


async def test_a_tool_server_that_did_not_connect_is_a_warning(tmp_path):
    context, events, _ = make_context(tmp_path)
    await claude.Messages(context).handle(SystemMessage(subtype="init", data={**INIT.data, "mcp_servers": [{"name": "desktop", "status": "connected"}, {"name": "browser", "status": "failed"}]}))
    assert [(event["level"], event["message"]) for event in events] == [
        ("ok", "Claude Code session started."), ("warn", "Claude Code started without its browser tools."),
    ]


async def test_settings_check_probes_the_claude_agent(fake_claude, tmp_path, monkeypatch):
    async def agent(default_model):
        return {"role": "agent", "label": "Task agent", "ok": True}

    installed = tmp_path / "chrome"
    installed.touch()

    async def chromium():
        return str(installed)

    monkeypatch.setattr(health, "chromium_path", chromium)
    common = {"default_model": "m", "client_factory": lambda *a, **k: FakeClient(["OK"]), "agent": agent,
              "observer": lambda: {"role": "observer", "label": "Observer", "ok": True}}
    checked = await check_settings(fake_settings(actor_agents=("claude",), actor_claude_model="sonnet"), **common)
    assert [item["role"] for item in checked["checks"]] == ["summary", "proposer", "belief", "preference", "observer", "claude", "prices"]
    assert checked["checks"][-2] == {**checked["checks"][-2], "ok": True, "model": "sonnet", "detail": "claude 2.1.293 (Claude Code) · npx 10.9.2 · chromium installed"}
    assert "No price for fake-model:" in checked["checks"][-1]["error"]  # the desktop agent's model is not asked for a price
    installed.unlink()  # without Playwright's Chromium the check says what to run
    missing = await check_settings(fake_settings(actor_agents=("claude",)), **common)
    assert missing["checks"][-2]["ok"] is False and "pnpm python:playwright:install" in missing["checks"][-2]["error"]


async def test_settings_check_reports_a_claude_that_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "claude_cli", lambda: str(tmp_path / "no-such-claude"))
    probed = await probe_claude(fake_settings(actor_agents=("claude",)))
    assert probed["ok"] is False and "no-such-claude" in probed["error"]


# ----- the desktop tool server -----------------------------------------------------------------------------------

def png(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


PNG = png(3136, 2)  # twice the long edge the server sends


class FakeCell:
    def __init__(self, runtime, items):
        self.runtime, self.items, self.id, self.delivered, self.done = runtime, list(items), "c1", 0, False

    async def next(self, timeout_s):
        if self.items:
            self.delivered += 1
            return {"progress": self.items.pop(0)}
        self.done = True
        self.runtime.cell = None
        return {"output": [{"type": "input_text", "text": "logged"}, {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}, {"type": "input_text", "text": "cell done"}]}

    def interrupt(self):
        self.items.clear()


class FakeRuntime:
    def __init__(self):
        self.cell = None
        self.codes: list[str] = []
        self.closed = False

    async def start(self, code, signal=None):
        self.codes.append(code)
        self.cell = FakeCell(self, [{"type": "input_text", "text": "logged"}, {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}])
        return self.cell

    async def capture(self, signal=None):
        return {"image": PNG, "app": "", "title": ""}

    async def close(self):
        self.closed = True


async def test_the_desktop_server_runs_exec_py_in_the_worker_and_answers_with_its_output(monkeypatch, tmp_path):
    runtime = FakeRuntime()

    async def launch():
        return runtime

    monkeypatch.setenv("CUA_DESKTOP_FRAMES", str(tmp_path / "frames"))
    monkeypatch.setattr(desktop_server.runtime, "launch_python_runtime", launch)
    desktop = desktop_server.Desktop()
    opened = await desktop_server.answer(desktop, {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert opened["result"]["protocolVersion"] == "2025-06-18" and opened["result"]["capabilities"] == {"tools": {}}
    listed = (await desktop_server.answer(desktop, {"method": "tools/list"}))["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["exec_py", "wait_cell", "stop_cell"]
    assert "ax.get_state" in listed[0]["inputSchema"]["properties"]["code"]["description"] and desktop.runtimes == {}  # started on the first call
    ran = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {"code": "log(1)"}}})
    logged, image, done = ran["result"]["content"]
    assert runtime.codes == ["log(1)"] and (logged, done) == ({"type": "text", "text": "logged"}, {"type": "text", "text": "cell done"})
    shrunk = Image.open(io.BytesIO(base64.b64decode(image["data"])))
    assert image["mimeType"] == "image/jpeg" and shrunk.format == "JPEG" and shrunk.size == (1568, 1)  # a JPEG no larger than the server's long edge
    assert [path.suffix for path in (tmp_path / "frames").iterdir()] == [".png"]  # the window after the step, left for the run's record, not for the model
    refused = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {"code": ""}}})
    assert refused["result"]["isError"] and "nonempty" in refused["result"]["content"][0]["text"]
    none = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "wait_cell", "arguments": {"cell": "c9", "timeout_s": 1}}})
    assert none["result"]["content"] == [{"type": "text", "text": "No running cell c9."}]
    assert (await desktop_server.answer(desktop, {"method": "resources/list"}))["error"]["code"] == -32601


async def test_the_desktop_server_routes_exec_py_to_the_desktop_the_target_names(monkeypatch):
    local, remote = FakeRuntime(), FakeRuntime()

    async def launch():
        return local

    async def connect(provider, url, token):
        assert (provider, url, token) == ("cua", "wss://desk.example", "secret-token")
        return remote

    monkeypatch.setenv("CUA_ACTOR_DESKTOP_SOURCE", "local,remote")
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_PROVIDER", "cua")
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_URL", "wss://desk.example")
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_TOKEN", "secret-token")
    monkeypatch.setattr(desktop_server.runtime, "launch_python_runtime", launch)
    monkeypatch.setattr(desktop_server.remote_desktops, "connect", connect)
    desktop = desktop_server.Desktop()
    listed = (await desktop_server.answer(desktop, {"method": "tools/list"}))["result"]["tools"]
    assert listed[0]["inputSchema"]["properties"]["target"]["enum"] == ["local", "remote"]
    refused = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {"code": "log(1)"}}})
    assert refused["result"]["isError"] and "target" in refused["result"]["content"][0]["text"]
    on_remote = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {"code": "log(2)", "target": "remote"}}})
    on_local = await desktop_server.answer(desktop, {"method": "tools/call", "params": {"name": "exec_py", "arguments": {"code": "log(3)", "target": "local"}}})
    assert (remote.codes, local.codes) == (["log(2)"], ["log(3)"])
    assert on_remote["result"]["content"][-1]["mimeType"] == "image/jpeg"  # the screen after the step
    assert on_local["result"]["content"][-1] == {"type": "text", "text": "cell done"}
    await desktop.close()
    assert local.closed and remote.closed


async def test_the_desktop_server_speaks_json_rpc_over_stdio(tmp_path):
    """The server as Claude starts it: a request line in, a reply line out; a notification gets none."""
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.live.actor.standalone.desktop_server", stdin=asyncio.subprocess.PIPE,
                                                   stdout=asyncio.subprocess.PIPE, cwd=tmp_path)
    out, _ = await asyncio.wait_for(process.communicate("".join(json.dumps(request) + "\n" for request in requests).encode()), 30)
    replies = [json.loads(line) for line in out.decode().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2] and replies[0]["result"]["serverInfo"]["name"] == "computerpilot-desktop"
    assert [tool["name"] for tool in replies[1]["result"]["tools"]] == ["exec_py", "wait_cell", "stop_cell"] and process.returncode == 0
