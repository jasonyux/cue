import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import RunnerCoreError
from app.lab_catalog import list_scenarios
from app.runner import RunnerManager


@pytest.fixture
async def manager_factory(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    (template / "index.html").write_text("<h1>Fixture</h1>")
    scenarios = [{**scenario, "workspaceTemplatePath": str(template)} for scenario in list_scenarios()]
    managers = []
    def create(executor, **kwargs):
        manager = RunnerManager(tmp_path / f"data-{len(managers)}", scenarios=scenarios,
                                executor=executor, **kwargs)
        managers.append(manager)
        return manager
    yield create
    for manager in managers:
        await manager.shutdown()


async def idle(context):
    await context.signal.wait()


async def wait_for_run_status(manager, run_id, status):
    async def wait():
        while True:
            detail = await manager.get_run_detail(run_id)
            if detail["run"]["status"] == status:
                return detail
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(wait(), 5)


def request(**kwargs):
    return {"scenarioId": "desktop-free-task", "prompt": "Move the cards", **kwargs}


async def test_single_admission_is_reserved_before_workspace_copy(manager_factory, monkeypatch):
    manager = manager_factory(idle)
    admitted, release = asyncio.Event(), asyncio.Event()
    prepare = manager.storage.prepare
    async def slow_prepare(*args):
        admitted.set()
        await release.wait()
        await prepare(*args)
    monkeypatch.setattr(manager.storage, "prepare", slow_prepare)
    start = asyncio.create_task(manager.start_run(request()))
    await admitted.wait()
    with pytest.raises(RunnerCoreError, match="already active"):
        await manager.start_run(request())
    lookup = asyncio.create_task(manager.get_active_run_detail())
    await asyncio.sleep(0)
    assert not lookup.done()
    release.set()
    detail = await start
    assert (await lookup)["run"]["id"] == detail["run"]["id"]
    detail["events"].clear()
    assert (await manager.get_run_detail(detail["run"]["id"]))["events"]


async def test_success_waits_for_executor_cleanup(manager_factory):
    completed, cleanup = asyncio.Event(), asyncio.Event()
    async def executor(context):
        await context.complete_run(notes=["done"])
        completed.set()
        await cleanup.wait()
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    await completed.wait()
    assert (await manager.get_run_detail(detail["run"]["id"]))["run"]["status"] == "running"
    with pytest.raises(RunnerCoreError):
        await manager.start_run(request())
    cleanup.set()
    final = await wait_for_run_status(manager, detail["run"]["id"], "completed")
    replay = await manager.get_replay_bundle(detail["run"]["id"])
    assert replay["events"][-1]["type"] == "run_completed"
    assert replay["run"] == final["run"]
    assert set(final["run"]["summary"]) == {"notes", "stepCount", "screenshotCount"}
    assert await manager.get_active_run_detail() is None


async def test_overlapping_stop_and_shutdown_wait_for_one_cleanup(manager_factory):
    running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanups = []
    async def executor(context):
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleanups.append(True)
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    await running.wait()
    stop = asyncio.create_task(manager.stop_run(detail["run"]["id"]))
    await cleaning.wait()
    shutdown = asyncio.create_task(manager.shutdown())
    assert (await manager.get_run_detail(detail["run"]["id"]))["run"]["status"] == "running"
    assert manager.active_id
    release.set()
    final, _ = await asyncio.gather(stop, shutdown)
    assert final["run"]["status"] == "cancelled"
    assert cleanups == [True]
    assert [event["type"] for event in final["events"]].count("run_cancelled") == 1


@pytest.mark.parametrize("code,blocked", [("cleanup_failed", False), ("desktop_cleanup_failed", True)])
async def test_stop_reports_cleanup_failure_and_desktop_lockout(manager_factory, code, blocked):
    started = asyncio.Event()
    async def executor(context):
        started.set()
        try:
            await context.signal.wait()
        finally:
            raise RunnerCoreError("cleanup broken", code=code)
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    await started.wait()
    final = await manager.stop_run(detail["run"]["id"])
    assert final["run"]["status"] == "failed"
    assert manager.desktop_recovery_required is blocked
    if blocked:
        with pytest.raises(RunnerCoreError) as caught:
            await manager.start_run(request())
        assert caught.value.code == "desktop_cleanup_failed"
    else:
        manager.executor = idle
        assert (await manager.start_run(request()))["run"]["status"] == "running"


async def test_failsafe_cancellation_and_missing_completion(manager_factory):
    async def failsafe(context):
        raise RunnerCoreError("corner", code="python_failsafe")
    manager = manager_factory(failsafe)
    detail = await manager.start_run(request())
    final = await wait_for_run_status(manager, detail["run"]["id"], "cancelled")
    assert final["run"]["status"] == "cancelled"
    manager.executor = AsyncMock()
    second = await manager.start_run(request())
    failed = await wait_for_run_status(manager, second["run"]["id"], "failed")
    assert "without completing" in failed["run"]["summary"]["notes"][0]


async def test_cleanup_error_after_complete_never_publishes_success(manager_factory):
    async def executor(context):
        await context.complete_run(notes=[])
        raise RunnerCoreError("Browser cleanup failed", code="cleanup_failed")
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    final = await wait_for_run_status(manager, detail["run"]["id"], "failed")
    assert not any(event["type"] == "run_completed" for event in final["events"])


async def test_terminal_replay_failure_keeps_truthful_live_result(manager_factory, monkeypatch):
    from app import storage
    original = storage.atomic_json
    async def executor(context):
        await context.complete_run(notes=[])
    def fail_terminal(path, value):
        if path.name == "replay.json" and value["run"]["status"] in ("completed", "failed"):
            raise OSError("disk full")
        return original(path, value)
    manager = manager_factory(executor)
    monkeypatch.setattr(storage, "atomic_json", fail_terminal)
    detail = await manager.start_run(request())
    final = await wait_for_run_status(manager, detail["run"]["id"], "failed")
    assert "disk full" in str(final["run"]["summary"]["notes"])
    assert await manager.get_active_run_detail() is None
    json.loads((manager.storage.run_dir(detail["run"]["id"]) / "replay.json").read_text())


async def test_cancelled_write_finishes_before_terminal_snapshot(manager_factory, monkeypatch):
    from app import storage
    original = storage.atomic_json
    writing = threading.Event()
    release = threading.Event()
    async def executor(context):
        await context.emit_event({"type": "run_progress", "message": "block write", "level": "ok"})
        await context.signal.wait()
    def hold_progress(path, value):
        if path.name == "replay.json" and value["events"] and value["events"][-1]["message"] == "block write":
            writing.set()
            assert release.wait(5)
        original(path, value)
    manager = manager_factory(executor)
    monkeypatch.setattr(storage, "atomic_json", hold_progress)
    detail = await manager.start_run(request())
    assert await asyncio.to_thread(writing.wait, 5)
    stopping = asyncio.create_task(manager.stop_run(detail["run"]["id"]))
    await asyncio.sleep(0.02)
    assert not stopping.done()
    release.set()
    await stopping
    replay = await manager.get_replay_bundle(detail["run"]["id"])
    assert replay["run"]["status"] == "cancelled"
    assert replay["events"][-1]["type"] == "run_cancelled"


async def test_subscriber_exception_and_replay_reload(manager_factory):
    release = asyncio.Event()
    async def executor(context):
        await release.wait()
        await context.complete_run(notes=[])
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    def broken(event):
        raise RuntimeError("Subscriber bug")
    seen = []
    manager.subscribe(detail["run"]["id"], broken)
    manager.subscribe(detail["run"]["id"], seen.append)
    release.set()
    final = await wait_for_run_status(manager, detail["run"]["id"], "completed")
    assert seen[-1]["type"] == "run_completed"
    manager.contexts.clear()
    assert (await manager.get_run_detail(detail["run"]["id"])) == final


async def test_screenshot_artifacts_carry_every_model_field_and_stored_placeholders_are_recognized(manager_factory):
    from app.runner import timestamp

    class Session:
        """A desktop session whose captures are the placeholder canvas; only the first one carries the flag."""

        mode = "headful"
        target_label = "local macOS apps"

        def __init__(self):
            self.viewport = {"width": 640, "height": 120}
            self.count = 0

        async def read_state(self):
            return {"currentUrl": "desktop://local", "pageTitle": "Calculator"}

        async def capture_screenshot(self, label):
            self.count += 1
            record = {"currentUrl": "desktop://local", "pageTitle": "Calculator", "id": f"screenshot-{self.count}", "label": label,
                      "path": f"/tmp/{label}.png", "mimeType": "image/png", "capturedAt": timestamp(), "ignored": "x"}
            return {**record, "placeholder": True} if self.count == 1 else record

    async def executor(context):
        session = Session()
        await context.capture_screenshot(session, "desktop-start")
        await context.capture_screenshot(session, "desktop-final")
        await context.complete_run(notes=["done"])

    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    final = await wait_for_run_status(manager, detail["run"]["id"], "completed")
    live = final["browser"]["screenshots"]
    assert [shot.get("placeholder") for shot in live] == [True, None] and all("ignored" not in shot for shot in live)
    assert live[0]["pageTitle"] == "Calculator" and live[0]["imageWidth"] == 640 and live[0]["source"] == "browser_preview"
    manager.contexts.clear()  # read back from disk: a pre-flag record is recognized by its canvas
    stored = (await manager.get_run_detail(detail["run"]["id"]))["browser"]["screenshots"]
    assert [shot.get("placeholder") for shot in stored] == [True, True]


async def test_run_storage_locates_runs_kept_in_session_folders(tmp_path):
    from app.storage import RunStorage

    storage = RunStorage(tmp_path)
    assert storage.run_dir("abc") == tmp_path / "runs" / "abc"  # nothing on disk yet: the default place
    stored = tmp_path / "sessions" / "20260921-170000" / "runs" / "abc"
    stored.mkdir(parents=True)
    assert storage.run_dir("abc") == stored and storage.locations["abc"] == stored  # discovered, then cached
    template = tmp_path / "template"
    template.mkdir()
    inside = tmp_path / "sessions" / "20260921-170000" / "runs" / "xyz"
    await storage.prepare("xyz", str(template), inside)
    assert storage.run_dir("xyz") == inside and (inside / "events.jsonl").is_file() and not (tmp_path / "runs" / "xyz").exists()
    with pytest.raises(RunnerCoreError):
        storage.run_dir("../outside")


@pytest.mark.parametrize("saved", [[], None, "text", {"version": 3}])
async def test_malformed_current_replay_returns_not_found(manager_factory, saved):
    manager = manager_factory(idle)
    directory = manager.storage.run_dir("historical")
    directory.mkdir(parents=True)
    (directory / "replay.json").write_text(json.dumps(saved))
    with pytest.raises(RunnerCoreError) as caught:
        await manager.get_replay_bundle("historical")
    assert caught.value.code == "run_not_found"
    assert caught.value.status_code == 404


async def test_headless_invalid_ids_and_a_fresh_workspace_per_run(manager_factory):
    manager = manager_factory(idle)
    with pytest.raises(RunnerCoreError) as caught:
        await manager.start_run(request(browserMode="headless"))
    assert caught.value.code == "visible_browser_required"
    assert not manager.storage.root.exists()
    with pytest.raises(RunnerCoreError):
        await manager.get_run_detail("../outside")
    detail = await manager.start_run(request())
    (Path(detail["workspacePath"]) / "index.html").write_text("mutated")
    assert (await manager.stop_run(detail["run"]["id"]))["run"]["status"] == "cancelled"
    fresh = await manager.start_run(request())
    assert (Path(fresh["workspacePath"]) / "index.html").read_text() == "<h1>Fixture</h1>"


async def test_exact_model_image_metadata(manager_factory):
    async def executor(context):
        session = SimpleNamespace(mode="headful", viewport={"width": 1440, "height": 900}, target_label="lab",
                                  read_state=AsyncMock(return_value={"currentUrl": "http://127.0.0.1/lab", "pageTitle": "Lab"}))
        await context.capture_screenshot(session, "image", {"base64": "YWJj", "width": 100, "height": 50})
        await context.complete_run(notes=[])
    manager = manager_factory(executor)
    detail = await manager.start_run(request())
    final = await wait_for_run_status(manager, detail["run"]["id"], "completed")
    image = final["browser"]["screenshots"][0]
    assert Path(image["path"]).read_bytes() == b"abc"
    assert (image["source"], image["imageWidth"], image["imageHeight"]) == ("code_tool", 100, 50)


def test_run_ids_sort_by_start_time_and_stay_url_safe():
    import re

    from app.runner import new_run_id
    from app.storage import validate_run_id
    first, second = new_run_id(), new_run_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", first)
    assert validate_run_id(first) == first
    assert first != second and first[:15] <= second[:15]
