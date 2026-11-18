"""Actor boundary: what task runs receive from a session and where they are stored."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import RunnerCoreError
from app.live.permissions import Level, Permissions
from app.live.storage.records import as_summaries
from app.runner import RunnerManager
from app.scenario_runtime import RunExecutionContext, execute_scenario


async def test_runs_receive_live_context_and_are_stored_in_the_session(tmp_path):
    class FakeLive:
        def __init__(self):
            self.recorded = []
            self.finished = []
            self.folder = tmp_path / "data" / "sessions" / "20260921-170000" / "runs"

        def actor_inputs(self):
            return {"worklog": as_summaries([{"windowStart": "2026-09-21T17:00:00Z", "windowEnd": "2026-09-21T17:01:00Z",
                                              "summary": "The user was editing budget.xlsx"}]), "proposal": None}

        def run_directory(self, run_id):
            return self.folder / run_id

        def session_folder(self):
            return self.folder.parent

        def require_running(self):
            pass  # never paused

        def actor_yield_s(self):
            return 20.0

        def actor_agents(self):
            return ("desktop",)

        def cmux_settings(self):
            return {}

        def desktop_source(self):
            return ("local",)

        def desktop_remote(self):
            return None

        def record_run(self, run_id, context, prompt="", chat=None, proposal=None):
            self.recorded.append((run_id, context, prompt, proposal))

        def record_run_finished(self, run_id, status, chat=None):
            self.finished.append((run_id, status))

    seen = {}

    async def execute(context):
        seen["liveContext"] = context.detail.get("liveContext")
        await context.complete_run(notes=["done"])

    template = tmp_path / "template"
    template.mkdir()
    scenarios = [{"id": "desktop-free-task", "labId": "desktop", "category": "productivity", "title": "Desktop",
                  "description": "d", "defaultPrompt": "p", "workspaceTemplatePath": str(template), "tags": ["desktop"]}]
    live = FakeLive()
    manager = RunnerManager(tmp_path / "data", executor=execute, scenarios=scenarios, live=live)
    proposal = {"proposalsTs": "2026-09-21T17:05:00Z", "choice": 0}
    detail = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "finish it", "liveContext": True, "proposal": proposal})
    run_id = detail["run"]["id"]
    await manager.contexts[run_id].execution
    assert detail["run"]["proposal"] == proposal and live.recorded[0][3] == proposal  # the run keeps which proposal it carries out
    assert detail["liveContext"].startswith("Observed context") and "editing budget.xlsx" in detail["liveContext"]
    assert seen["liveContext"] == detail["liveContext"]  # the run record keeps the text the agent was built from
    assert detail["actorYieldS"] == 20.0 and "actorYieldS" not in (await manager.get_run_detail(run_id))["run"]
    assert detail["actorAgent"] == "desktop"
    with pytest.raises(RunnerCoreError, match="did not set up the cmux agent"):  # only the agents the session set up can run
        await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "finish it", "liveContext": True, "agent": "cmux"})
    with pytest.raises(RunnerCoreError, match="runs from a continuous session"):
        await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "finish it", "agent": "cmux"})
    assert live.recorded == [(run_id, detail["liveContext"], "finish it", proposal)] and live.finished == [(run_id, "completed")]
    # Artifacts live in the session folder, and a fresh storage index (a restarted backend) finds them there again.
    assert manager.storage.run_dir(run_id) == live.folder / run_id and (live.folder / run_id / "replay.json").is_file()
    assert not (tmp_path / "data" / "runs" / run_id).exists()
    manager.storage.locations.clear()
    assert manager.storage.run_dir(run_id) == live.folder / run_id
    replay = await manager.get_replay_bundle(run_id)
    assert replay["run"]["status"] == "completed" and replay["artifacts"]["replayPath"] == str(live.folder / run_id / "replay.json")
    plain = await manager.start_run({"scenarioId": "desktop-free-task", "prompt": "no context"})
    await manager.contexts[plain["run"]["id"]].execution
    assert "liveContext" not in plain and len(live.recorded) == 1 and live.finished[-1] == (plain["run"]["id"], "completed")
    assert manager.storage.run_dir(plain["run"]["id"]) == tmp_path / "data" / "runs" / plain["run"]["id"]


BUDGET = {"worklog": as_summaries([{"windowStart": "2026-09-21T17:00:00Z", "windowEnd": "2026-09-21T17:01:00Z",
                                    "summary": "budget.xlsx open in Numbers"}]), "proposal": None}


async def test_scenario_runtime_appends_live_context_to_instructions(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    runtime = SimpleNamespace(close=AsyncMock(), mode="accessibility", platform="darwin", capture=AsyncMock(return_value={"image": None}))
    loop = AsyncMock(return_value={"notes": ["ok"]})
    scenario_loop = AsyncMock(return_value={"notes": ["ok"]})
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.live.actor.desktop.agent_loop.run_agent_loop", loop)  # a run with live context uses the live actor
    monkeypatch.setattr("app.responses_loop.run_responses_code_loop", scenario_loop)  # the scenario page's agent stays out of it
    launch_actor_worker = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.live.actor.desktop.runtime.launch_python_runtime", launch_actor_worker)  # the actor's own worker
    monkeypatch.setattr("app.desktop.runtime.launch_python_runtime", AsyncMock(side_effect=AssertionError("scenario worker used")))
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-free-task", "labId": "desktop"}])
    context = RunExecutionContext(
        detail={"run": {"labId": "desktop", "scenarioId": "desktop-free-task", "browserMode": "headful", "prompt": "go",
                        "model": "fixture", "maxResponseTurns": 4}, "workspacePath": str(tmp_path), "liveContext": "Observed context", "actorAgent": "desktop"},
        signal=asyncio.Event(), screenshot_directory=tmp_path / "run" / "screenshots", emit_event=AsyncMock(),
        capture_screenshot=AsyncMock(), sync_browser_state=AsyncMock(), complete_run=AsyncMock(), actor_inputs=BUDGET)
    await execute_scenario(context)
    instructions = loop.call_args.kwargs["instructions"]
    assert instructions.endswith("budget.xlsx open in Numbers") and "Accessibility API" in instructions
    assert scenario_loop.call_count == 0 and launch_actor_worker.call_count == 1
    assert (tmp_path / "run" / "live_context.md").read_text(encoding="utf-8").startswith("Observed context")


async def test_scenario_runtime_connects_the_remote_desktop_for_a_live_run(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    runtime = SimpleNamespace(close=AsyncMock(), mode="pyautogui", platform="darwin", capture=AsyncMock(return_value={"image": None}))
    remote = SimpleNamespace(close=AsyncMock(), mode="pyautogui", platform="linux")
    loop = AsyncMock(return_value={"notes": ["ok"]})
    connect = AsyncMock(return_value=remote)
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.live.actor.desktop.agent_loop.run_agent_loop", loop)
    monkeypatch.setattr("app.live.actor.desktop.runtime.launch_python_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr("app.live.actor.desktop.remote.connect", connect)
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-free-task", "labId": "desktop"}])
    context = RunExecutionContext(
        detail={"run": {"labId": "desktop", "scenarioId": "desktop-free-task", "browserMode": "headful", "prompt": "go",
                        "model": "fixture", "maxResponseTurns": 4}, "workspacePath": str(tmp_path), "liveContext": "Observed context", "actorAgent": "desktop"},
        signal=asyncio.Event(), screenshot_directory=tmp_path / "run" / "screenshots", emit_event=AsyncMock(),
        capture_screenshot=AsyncMock(), sync_browser_state=AsyncMock(), complete_run=AsyncMock(), actor_inputs=BUDGET,
        desktop_source=("local", "remote"), desktop_remote=("cua", "wss://desk", "t"))
    await execute_scenario(context)
    assert connect.call_args.args == ("cua", "wss://desk", "t") and loop.call_args.kwargs["session"].remote is remote
    assert loop.call_args.kwargs["session"].execution is runtime
    assert "isolated Ubuntu desktop" in loop.call_args.kwargs["instructions"] and remote.close.await_count == 1


async def test_scenario_runtime_can_run_on_the_remote_desktop_alone(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    remote = SimpleNamespace(close=AsyncMock(), mode="pyautogui", platform="linux", capture=AsyncMock(return_value={"image": None}))
    loop = AsyncMock(return_value={"notes": ["ok"]})
    launch_local = AsyncMock(side_effect=AssertionError("the local worker must not start"))
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.live.actor.desktop.agent_loop.run_agent_loop", loop)
    monkeypatch.setattr("app.live.actor.desktop.runtime.launch_python_runtime", launch_local)
    monkeypatch.setattr("app.live.actor.desktop.remote.connect", AsyncMock(return_value=remote))
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-free-task", "labId": "desktop"}])
    context = RunExecutionContext(
        detail={"run": {"labId": "desktop", "scenarioId": "desktop-free-task", "browserMode": "headful", "prompt": "go",
                        "model": "fixture", "maxResponseTurns": 4}, "workspacePath": str(tmp_path), "liveContext": "Observed context", "actorAgent": "desktop"},
        signal=asyncio.Event(), screenshot_directory=tmp_path / "run" / "screenshots", emit_event=AsyncMock(),
        capture_screenshot=AsyncMock(), sync_browser_state=AsyncMock(), complete_run=AsyncMock(), actor_inputs=BUDGET,
        desktop_source=("remote",), desktop_remote=("cua", "wss://desk", "t"))
    await execute_scenario(context)
    session = loop.call_args.kwargs["session"]
    assert session.execution is remote and session.remote is None and launch_local.await_count == 0  # one desktop: no target
    assert "isolated Ubuntu desktop" in loop.call_args.kwargs["instructions"] and remote.close.await_count == 1


async def test_a_live_desktop_run_tells_its_worker_the_session_and_the_model_the_rules(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    runtime = SimpleNamespace(close=AsyncMock(), mode="accessibility", platform="darwin", capture=AsyncMock(return_value={"image": None}))
    loop = AsyncMock(return_value={"notes": ["ok"]})
    launch = AsyncMock(return_value=runtime)
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.live.actor.desktop.agent_loop.run_agent_loop", loop)
    monkeypatch.setattr("app.live.actor.desktop.runtime.launch_python_runtime", launch)
    monkeypatch.setattr("app.live.actor.run.agent_rules", lambda folder: SimpleNamespace(current=lambda: Permissions(Level.LOOK, {"Slack": Level.USE})))
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-free-task", "labId": "desktop"}])
    context = RunExecutionContext(
        detail={"run": {"labId": "desktop", "scenarioId": "desktop-free-task", "browserMode": "headful", "prompt": "go",
                        "model": "fixture", "maxResponseTurns": 4}, "workspacePath": str(tmp_path), "liveContext": "Observed context", "actorAgent": "desktop",
                "actorSessionFolder": str(tmp_path / "session")},
        signal=asyncio.Event(), screenshot_directory=tmp_path / "run" / "screenshots", emit_event=AsyncMock(),
        capture_screenshot=AsyncMock(), sync_browser_state=AsyncMock(), complete_run=AsyncMock(), actor_inputs=BUDGET)
    await execute_scenario(context)
    assert launch.call_args.kwargs["env"] == {"CUA_SESSION_FOLDER": str(tmp_path / "session")}  # where ax reads the rules and writes its asks
    told = loop.call_args.kwargs["instructions"]
    assert "Now: apps not listed: look; use: Slack. A refused call raises AccessibilityError" in told and "ax.ask('App', 'use', why='what for')" in told
