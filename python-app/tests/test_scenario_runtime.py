import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import RunnerCoreError
from app.scenario_runtime import RunExecutionContext, execute_scenario


@pytest.fixture
def flow(monkeypatch, tmp_path):
    client = SimpleNamespace(close=AsyncMock())
    worker = SimpleNamespace(close=AsyncMock(), mode="accessibility")
    session = SimpleNamespace(close=AsyncMock(), target_label="desktop")
    launch_worker = AsyncMock(return_value=worker)
    monkeypatch.setattr("app.responses_loop.create_default_responses_client", lambda: client)
    monkeypatch.setattr("app.responses_loop.run_responses_code_loop", AsyncMock(return_value={"notes": ["model final"]}))
    monkeypatch.setattr("app.desktop.runtime.launch_python_runtime", launch_worker)
    monkeypatch.setattr("app.scenario_runtime.DesktopSession", lambda **_: session)
    monkeypatch.setattr("app.scenario_runtime.list_scenarios", lambda: [{"id": "desktop-free-task", "labId": "desktop"}])
    monkeypatch.setattr("app.scenario_runtime.instructions", lambda scenario_id, mode: "instructions")
    def context():
        run = {"labId": "desktop", "scenarioId": "desktop-free-task", "browserMode": "headful", "prompt": "Do the task", "model": "fixture", "maxResponseTurns": 24}
        return RunExecutionContext(
            detail={"run": run, "workspacePath": str(tmp_path)},
            signal=asyncio.Event(),
            screenshot_directory=Path(tmp_path),
            emit_event=AsyncMock(),
            capture_screenshot=AsyncMock(),
            sync_browser_state=AsyncMock(),
            complete_run=AsyncMock(),
        )
    return SimpleNamespace(context=context, worker=worker, session=session, client=client, launch_worker=launch_worker)


async def test_a_scenario_finishes_only_with_awaited_resource_cleanup(flow):
    context = flow.context()
    await execute_scenario(context)
    flow.worker.close.assert_awaited_once()
    flow.session.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()
    context.complete_run.assert_awaited_once_with(notes=["model final"])
    assert [call.args[1] for call in context.capture_screenshot.call_args_list] == ["desktop-start", "desktop-final"]


async def test_unknown_scenario_fails_before_desktop_start(flow):
    context = flow.context()
    context.detail["run"]["scenarioId"] = "missing"
    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(context)
    assert caught.value.code == "unsupported_scenario"
    flow.launch_worker.assert_not_awaited()


async def test_a_failure_after_the_worker_starts_closes_worker_session_and_client(flow):
    context = flow.context()
    context.capture_screenshot.side_effect = RuntimeError("Screenshot failed")
    with pytest.raises(RuntimeError, match="Screenshot failed"):
        await execute_scenario(context)
    flow.worker.close.assert_awaited_once()
    flow.session.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()


async def test_failed_input_release_still_closes_all_resources(flow):
    flow.worker.close.side_effect = RunnerCoreError("Input release failed", code="input_release_failed")
    with pytest.raises(RunnerCoreError) as caught:
        await execute_scenario(flow.context())
    assert caught.value.code == "desktop_cleanup_failed"
    flow.session.close.assert_awaited_once()
    flow.client.close.assert_awaited_once()


async def test_repeated_cancellation_does_not_abandon_cleanup(flow):
    entered, release = asyncio.Event(), asyncio.Event()
    async def close():
        entered.set()
        await release.wait()
    flow.worker.close.side_effect = close
    task = asyncio.create_task(execute_scenario(flow.context()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await task
    flow.worker.close.assert_awaited_once()
    flow.session.close.assert_awaited_once()
