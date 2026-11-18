"""The codex agent: Codex through OpenAI's Codex SDK, its turn's notifications relayed as run events, with the same tool
servers as the claude agent and only those."""

import asyncio
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from live_helpers import FakeClient, fake_settings
from openai_codex import ApprovalMode, CodexError, Sandbox
from PIL import Image
from pydantic import BaseModel
from test_live_standalone import ENDPOINT, NOTES, FakeLive, make_context

from app.errors import RunnerCoreError
from app.live import health
from app.live.actor.prompts import STANDALONE_INTROS, ActorInput, instructions
from app.live.actor.standalone import browser, codex
from app.live.actor.standalone.servers import PLAYWRIGHT_MCP
from app.live.health import check_settings
from app.runner import RunnerManager


def png(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class Payload(BaseModel):
    """A notification's payload as the SDK hands it over: a model that dumps to the wire's camelCase."""

    data: dict

    def model_dump(self, **kwargs):
        return self.data


def note(method: str, **data) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=Payload(data=data))


def usage(inputs: int, outputs: int, reasoning: int = 0) -> dict:
    return {"last": {"inputTokens": inputs, "cachedInputTokens": inputs // 2, "cacheWriteInputTokens": 0, "outputTokens": outputs,
                     "reasoningOutputTokens": reasoning, "totalTokens": inputs + outputs}}


BROWSER_CALL = {"type": "mcpToolCall", "id": "call-1", "server": "browser", "tool": "browser_navigate", "arguments": {"url": "https://example.com"}, "status": "inProgress"}
DESKTOP_CALL = {"type": "mcpToolCall", "id": "call-2", "server": "desktop", "tool": "exec_py", "arguments": {"code": "display(ax.get_state('Notes').screenshot)"}, "status": "inProgress"}
SHELL = {"type": "commandExecution", "id": "cmd-1", "command": "/bin/zsh -lc 'ls'", "commandActions": [{"command": "ls", "type": "listFiles"}], "status": "inProgress"}
NOTES_TURN = [
    note("item/started", item={"type": "userMessage", "id": "u1", "content": []}),
    note("item/completed", item={"type": "reasoning", "id": "r1", "summary": ["Look at the notes first."], "content": []}),
    note("item/completed", item={"type": "agentMessage", "id": "m1", "phase": "commentary", "text": "Looking at the window."}),
    note("item/started", item=SHELL),
    note("item/completed", item={**SHELL, "status": "completed", "aggregatedOutput": "notes.md", "exitCode": 0}),
    note("thread/tokenUsage/updated", tokenUsage=usage(1000, 20, 5)),
    note("item/started", item=BROWSER_CALL),
    note("item/completed", item={**BROWSER_CALL, "status": "completed", "result": {"content": [{"type": "text", "text": "### Page\n- Page URL: https://example.com/\n- Page Title: Example"}]}}),
    note("item/started", item=DESKTOP_CALL),
    note("item/autoApprovalReview/completed", review={"status": "approved", "riskLevel": "low", "rationale": "Reads a window."}),
    ("frame",),  # the desktop server leaves a frame of the Mac after the step
    note("item/completed", item={**DESKTOP_CALL, "status": "completed", "result": {"content": [{"type": "text", "text": "window tree"}, {"type": "image", "data": png(4, 3), "mimeType": "image/png"}]}}),
    note("thread/tokenUsage/updated", tokenUsage=usage(2000, 30)),
    note("item/completed", item={"type": "agentMessage", "id": "m2", "phase": "final_answer", "text": "Done."}),
    note("thread/tokenUsage/updated", tokenUsage=usage(2100, 5)),
    note("turn/completed", turn={"id": "turn-1", "status": "completed", "items": []}),
]
SEEN: list[str] = []


class FakeCodex:
    """Stands in for AsyncCodex: keeps its config and the thread options and prompt it was given, plays a turn's
    notifications, ("frame",) which leaves a frame of the Mac where the desktop server would, ("hang",) or ("fail-start",),
    and notes when it was closed."""

    turn_script: ClassVar[list] = []
    instances: ClassVar[list] = []

    def __init__(self, config):
        self.config = config
        self.started: dict | None = None
        self.resumed: tuple | None = None
        self.prompts: list[str] = []
        self.closed = False
        FakeCodex.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def thread(self):
        if FakeCodex.turn_script[:1] == [("fail-start",)]:
            raise CodexError("Not signed in")
        fake = self

        class Turn:
            async def stream(self):
                for step in FakeCodex.turn_script:
                    if step == ("frame",):
                        folder = Path((fake.started or fake.resumed[1])["config"]["mcp_servers"]["desktop"]["env"]["CUA_DESKTOP_FRAMES"])
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / "1.png").write_bytes(base64.b64decode(png(5, 1)))
                        continue
                    if step == ("hang",):
                        await asyncio.sleep(60)
                    await asyncio.sleep(0)
                    yield step

        class Thread:
            id = "thread-1"

            async def read(self):
                return SimpleNamespace(thread=SimpleNamespace(model="gpt-5.6-terra"))

            async def turn(self, prompt):
                fake.prompts.append(prompt)
                return Turn()

        return Thread()

    async def thread_start(self, **options):
        self.started = options
        return self.thread()

    async def thread_resume(self, thread_id, **options):
        self.resumed = (thread_id, options)
        return self.thread()


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    """The Codex SDK replaced by the fake, the session's agent browser by a stub, and a Codex home whose config has two
    MCP servers of the user's."""
    monkeypatch.setattr(codex, "AsyncCodex", FakeCodex)
    async def frame():  # the tab after a browser step, as the run files it
        return {"base64": png(2, 1), "width": 2, "height": 1}

    monkeypatch.setitem(browser.OPEN, ENDPOINT, SimpleNamespace(seen=lambda text: SEEN.append(text), frame=frame))
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.screenpipe]\ncommand = "npx"\n\n[mcp_servers.computer-use]\ncommand = "cua"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    FakeCodex.turn_script, FakeCodex.instances = list(NOTES_TURN), []
    SEEN.clear()
    return FakeCodex


async def test_a_codex_run_is_started_followed_and_stored(tmp_path, sdk):
    live = FakeLive(tmp_path / "data" / "sessions" / "20261002-150000" / "runs")
    manager = RunnerManager(tmp_path / "data", live=live)
    started = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "Tidy the notes", "maxResponseTurns": 6, "liveContext": True, "agent": "codex"})
    run_id = started["run"]["id"]
    assert started["actorAgent"] == "codex" and started["actorCodexSandbox"] == "workspace-write" and started["run"]["model"] == "codex-default"
    assert started["actorBrowserEndpoint"] == ENDPOINT and started["actorDesktopSource"] == ["local"] and "actorYieldS" not in started
    await manager.contexts[run_id].execution
    detail = await manager.get_run_detail(run_id)
    assert detail["run"]["status"] == "completed" and detail["run"]["model"] == "gpt-5.6-terra" and live.finished == [(run_id, "completed")]
    assert detail["run"]["summary"]["notes"] == ["Executed the task with Codex.", "Model final response: Done."]
    usage_ = detail["run"]["usage"]  # summed over the turn's model calls; Codex reports no cost, so it is the price table's
    assert (usage_["calls"], usage_["inputTokens"], usage_["cachedInputTokens"], usage_["outputTokens"], usage_["reasoningTokens"]) == (3, 5100, 2550, 55, 5)
    assert usage_["costUsd"] == 0.00627 and usage_["byModel"][0]["model"] == "gpt-5.6-terra"  # 2550 × $2 + 2550 cached × $0.20 + 55 out × $12, per 1M
    events = [(event["type"], event["message"], event.get("detail", "")) for event in detail["events"]]
    messages = [message for _, message, _ in events]
    order = ["Codex session started.", "Model reasoning.", "Model progress.", "Codex turn 1 completed.", "Codex's reviewer approved a step.", "Model returned a final response."]
    assert [messages.index(m) for m in order] == sorted(messages.index(m) for m in order)
    calls = [the_detail for kind, _, the_detail in events if kind == "function_call_requested"]
    assert calls == ['shell {"command": "ls"}', 'mcp__browser__browser_navigate {"url": "https://example.com"}',
                     'mcp__desktop__exec_py {"code": "display(ax.get_state(\'Notes\').screenshot)"}']  # named as the claude agent's are, so the monitor reads them alike
    assert ("tool_progress", "shell output.", "notes.md") in events and ("tool_progress", "mcp__desktop__exec_py output.", "window tree") in events
    assert [shot["imageWidth"] for shot in detail["browser"]["screenshots"]] == [2, 4, 5]  # the frame after the browser step, exec_py's image, then the frame of the Mac
    assert SEEN == ["### Page\n- Page URL: https://example.com/\n- Page Title: Example"]  # the monitor follows the agent's tab
    # Codex got the agent's instructions as developer instructions, the run's two servers (approved up front) and only
    # those: Codex's own browser and computer use off, their bundled plugins with them, the user's MCP servers disabled.
    [client] = sdk.instances
    folder = live.folder / run_id / "codex"
    inputs = ActorInput(agent="codex", task="Tidy the notes", input=NOTES, permissions="apps not listed: look")
    assert client.prompts == ["Tidy the notes"] and client.closed and client.resumed is None
    options = client.started
    assert options["developer_instructions"] == instructions(inputs) and instructions(inputs).startswith(STANDALONE_INTROS["codex"])
    assert (options["cwd"], options["sandbox"], options["model"], options["approval_mode"]) == (detail["workspacePath"], Sandbox.workspace_write, None, ApprovalMode.auto_review)
    servers = options["config"]["mcp_servers"]
    assert servers["desktop"] == {"command": sys.executable, "args": ["-m", "app.live.actor.standalone.desktop_server"],
                                  "env": {"CUA_ACTOR_DESKTOP_SOURCE": "local", "CUA_DESKTOP_FRAMES": str(folder / "frames"), "CUA_SESSION_FOLDER": str(live.folder.parent)},
                                  "default_tools_approval_mode": "approve"}
    assert servers["browser"]["args"] == ["-y", PLAYWRIGHT_MCP, "--cdp-endpoint", ENDPOINT, "--output-dir", str(folder / "browser")]
    assert servers["history"]["args"] == ["-m", "app.live.actor.standalone.history_server"] and servers["history"]["default_tools_approval_mode"] == "approve"
    assert client.config.cwd == detail["workspacePath"] and client.config.config_overrides == (
        "features.computer_use=false", "features.browser_use=false", "features.browser_use_external=false", "features.in_app_browser=false",
        'plugins."unified-computer-use@openai-bundled".enabled=false', 'plugins."computer-use@openai-bundled".enabled=false',
        'plugins."browser@openai-bundled".enabled=false', "mcp_servers.screenpipe.enabled=false", "mcp_servers.computer-use.enabled=false")
    assert (folder / "instructions.md").read_text() == f"{instructions(inputs)}\n"


async def test_a_later_chat_turn_resumes_the_thread_and_leaves_it_for_the_next(tmp_path, sdk):
    context, _, _ = make_context(tmp_path, actorAgent="codex", actorCodexSandbox="read-only", actorCodexModel="gpt-5.6-terra",
                                      actorCodexCwd=str(tmp_path), chat={"turn": 2, "resume": "thread-0"})
    outcome = await codex.run_codex_agent(context, ActorInput(agent="codex", task="And now the rest", input=None))
    [client] = sdk.instances
    thread_id, options = client.resumed
    assert thread_id == "thread-0" and client.started is None and (options["sandbox"], options["model"], options["cwd"]) == (Sandbox.read_only, "gpt-5.6-terra", str(tmp_path))
    assert outcome["final_assistant_message"] == "Done." and context.detail["chat"] == {"turn": 2, "resume": "thread-0", "codexThreadId": "thread-1", "reply": "Done."}


async def test_stopping_a_codex_run_closes_codex(tmp_path, sdk):
    sdk.turn_script = [*NOTES_TURN[:3], ("hang",)]
    context, events, _ = make_context(tmp_path, actorAgent="codex", actorCodexSandbox="workspace-write", chat={"turn": 1})
    task = asyncio.create_task(codex.run_codex_agent(context, ActorInput(agent="codex", task="Tidy the notes", input=None)))
    while not any(event["message"] == "Model progress." for event in events):
        await asyncio.sleep(0.02)
    context.signal.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    [client] = sdk.instances
    assert client.closed and context.detail["chat"]["codexThreadId"] == "thread-1"  # a stopped turn can still be resumed


async def test_a_failed_turn_fails_the_run_with_its_error(tmp_path, sdk):
    sdk.turn_script = [*NOTES_TURN[:3], note("turn/completed", turn={"id": "turn-1", "status": "failed", "error": {"message": "usage limit reached"}})]
    context, _, _ = make_context(tmp_path, actorAgent="codex", actorCodexSandbox="workspace-write")
    with pytest.raises(RuntimeError, match="Codex ended with failed: usage limit reached"):
        await codex.run_codex_agent(context, ActorInput(agent="codex", task="Tidy the notes", input=None))


async def test_a_codex_that_cannot_start_is_said_so(tmp_path, sdk):
    sdk.turn_script = [("fail-start",)]
    context, _, _ = make_context(tmp_path, actorAgent="codex", actorCodexSandbox="workspace-write")
    with pytest.raises(RunnerCoreError) as refused:
        await codex.run_codex_agent(context, ActorInput(agent="codex", task="Tidy the notes", input=None))
    assert refused.value.code == "agent_unavailable" and "codex login" in refused.value.hint


def test_without_a_codex_config_only_codex_s_own_switches_are_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nothing-here"))
    assert codex.launch_overrides() == (*(f"features.{name}=false" for name in codex.FEATURES_OFF), *(f'plugins."{name}".enabled=false' for name in codex.PLUGINS_OFF))


async def test_settings_check_probes_the_codex_agent(monkeypatch):
    async def agent(default_model):
        return {"role": "agent", "label": "Task agent", "ok": True}

    async def signed_in():
        return "me@example.com"

    async def tools():
        return "npx 10.9.2 · chromium installed"

    monkeypatch.setattr(health, "codex_account", signed_in)
    monkeypatch.setattr(health, "browser_tools", tools)
    common = {"default_model": "m", "client_factory": lambda *a, **k: FakeClient(["OK"]), "agent": agent,
              "observer": lambda: {"role": "observer", "label": "Observer", "ok": True}}
    checked = await check_settings(fake_settings(actor_agents=("codex",)), **common)
    assert [item["role"] for item in checked["checks"]] == ["summary", "proposer", "belief", "preference", "observer", "codex", "prices"]
    assert checked["checks"][-2]["ok"] and checked["checks"][-2]["detail"] == "codex signed in (me@example.com) · npx 10.9.2 · chromium installed"

    async def signed_out():
        raise RuntimeError("Codex is not signed in: run codex login.")

    monkeypatch.setattr(health, "codex_account", signed_out)
    missing = await check_settings(fake_settings(actor_agents=("codex",)), **common)
    assert missing["checks"][-2]["ok"] is False and "codex login" in missing["checks"][-2]["error"]


async def test_a_run_s_model_picked_in_the_chat_overrides_the_session_s(tmp_path, sdk):
    live = FakeLive(tmp_path / "data" / "sessions" / "20261002-150000" / "runs")
    manager = RunnerManager(tmp_path / "data", live=live)
    started = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "Tidy the notes", "maxResponseTurns": 6, "liveContext": True, "agent": "codex",
                                       "model": "gpt-6-luna"})
    assert started["actorCodexModel"] == "gpt-6-luna" and started["run"]["model"] == "gpt-6-luna"
    await manager.contexts[started["run"]["id"]].execution
    assert sdk.instances[0].started["model"] == "gpt-6-luna"
    claude_run = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "Tidy the notes", "maxResponseTurns": 6, "liveContext": True, "agent": "claude",
                                          "model": "opus"})
    assert claude_run["actorClaudeModel"] == "opus"  # in place of the session's sonnet
    manager.contexts[claude_run["run"]["id"]].execution.cancel()


async def test_the_console_reads_each_agent_s_models_once(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx
    from live_helpers import make_manager

    from app.api import create_app
    from app.live import api as live_api

    asked: list[str] = []

    async def codex_models():
        asked.append("codex")
        return [{"id": "gpt-6-luna", "label": "GPT-6-Luna", "detail": "Fast", "default": True}]

    monkeypatch.setitem(live_api.MODEL_LISTS, "codex", codex_models)
    monkeypatch.setattr(live_api, "MODELS", {})
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        first = await http.get("/api/live/agents/codex/models")
        again = await http.get("/api/live/agents/codex/models")
        assert first.json() == again.json() == {"models": [{"id": "gpt-6-luna", "label": "GPT-6-Luna", "detail": "Fast", "default": True}]} and asked == ["codex"]
        assert (await http.get("/api/live/agents/desktop/models")).status_code == 404


def test_with_a_remote_desktop_codex_is_told_to_forward_the_token_by_name(tmp_path):
    """Codex starts an MCP server with a bare environment (verified: ten variables, none of the backend's), so the desktop
    server is given the token's name to forward, never its value."""
    detail = {"run": {"startedAt": "2026-10-19T15:00:00Z"}, "actorDesktopSource": ["local", "remote"], "desktopRemoteProvider": "cua", "desktopRemoteUrl": "wss://desk", "actorBrowserEndpoint": ENDPOINT,
              "workspacePath": str(tmp_path), "actorCodexSandbox": "workspace-write"}
    inputs = ActorInput(agent="codex", task="Look", input=None)
    servers = codex.thread_options(detail, inputs, tmp_path)["config"]["mcp_servers"]
    assert servers["desktop"]["env_vars"] == ["CUA_DESKTOP_REMOTE_TOKEN"]
    assert servers["desktop"]["env"] == {"CUA_ACTOR_DESKTOP_SOURCE": "local,remote", "CUA_DESKTOP_FRAMES": str(tmp_path / "frames"), "CUA_DESKTOP_REMOTE_PROVIDER": "cua", "CUA_DESKTOP_REMOTE_URL": "wss://desk"}
    local = codex.thread_options({**detail, "actorDesktopSource": ["local"], "desktopRemoteProvider": None}, inputs, tmp_path)["config"]["mcp_servers"]
    assert "env_vars" not in local["desktop"]
