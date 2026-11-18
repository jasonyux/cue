"""Run-scoped setup, execution, screenshots, and awaited teardown."""
import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .desktop.ax.session import DesktopSession
from .errors import RunnerCoreError
from .lab_catalog import instructions, list_scenarios
from .live.actor.prompts import ActorInputs
from .processes import assert_active


@dataclass
class RunExecutionContext:
    detail: dict
    signal: asyncio.Event
    screenshot_directory: Path
    emit_event: Callable[..., Awaitable]
    capture_screenshot: Callable[..., Awaitable]
    sync_browser_state: Callable[..., Awaitable]
    complete_run: Callable[..., Awaitable]
    desktop_source: tuple[str, ...] = ("local",)  # a live run's desktops: local, remote, or both
    desktop_remote: tuple[str, str, str] | None = None  # its remote desktop, (provider, url, token), when the source includes remote
    actor_inputs: ActorInputs | None = None  # a live run's worklog and proposal: what the session shows the agent


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def close_resources(*resources: Any) -> None:
    """Close every owned resource that exists, then raise once if any failed: a desktop whose input could not be
    released gets its own code, so the runner blocks new runs until the operator checks the desktop."""
    errors = []
    for resource in resources:
        if resource is not None:
            try:
                await resource.close()
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 - attempt every owned resource cleanup
                errors.append(error)
    if errors:
        release_failed = any(getattr(error, "code", "") in ("input_release_failed", "input_release_timeout") for error in errors)
        raise RunnerCoreError(
            "Run cleanup failed: " + "; ".join(str(error) for error in errors),
            code="desktop_cleanup_failed" if release_failed else "cleanup_failed",
            hint="Release held keys and mouse buttons, check desktop input permissions, then restart the runner before starting another run." if release_failed else None,
        )


async def teardown(cleanup: Coroutine[Any, Any, None]) -> None:
    """Run the cleanup to the end: once teardown starts, a second cancellation must not abandon resources."""
    task = asyncio.create_task(cleanup)
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if task.done():
                await task
                return


async def execute_scenario(context: RunExecutionContext) -> None:
    from .desktop.runtime import launch_python_runtime
    from .responses_loop import create_default_responses_client, run_responses_code_loop

    run = context.detail["run"]
    if not any(item["id"] == run["scenarioId"] and item["labId"] == run["labId"] for item in list_scenarios()):
        raise RunnerCoreError(f"Unsupported public scenario: {run['scenarioId']}", code="unsupported_scenario", status_code=404)
    if context.detail.get("actorAgent"):
        # A run from the live page (a one-shot run or a chat turn): the actor's own agents, workers and prompts
        # (app/live/actor), free to diverge from this page's agent. Nothing below is used for it.
        from .live.actor.run import run_task

        await run_task(context)
        return
    client = create_default_responses_client()
    python: Any = None
    session: Any = None
    try:
        assert_active(context.signal)
        if run["browserMode"] == "headless":
            raise RunnerCoreError("Local PyAutoGUI requires a visible browser.", code="visible_browser_required", status_code=400)
        await context.emit_event({"type": "run_progress", "level": "ok", "detail": run["model"],
                                  "message": "Using the live Responses API code loop for the desktop."})
        python = await launch_python_runtime(signal=context.signal)
        mode = getattr(python, "mode", "pyautogui")
        assert_active(context.signal)
        # The scenarios operate local apps: screenshots come from the worker.
        session = DesktopSession(runtime=python, screenshot_dir=context.screenshot_directory,
                                 target_label=f"local macOS apps ({mode})", signal=context.signal)
        await context.sync_browser_state(session)
        await context.emit_event({"type": "browser_session_started", "level": "ok", "detail": session.target_label,
                                  "message": "Desktop session ready."})
        await context.capture_screenshot(session, "desktop-start")
        result = await run_responses_code_loop(context=context, session=session, instructions=instructions(run["scenarioId"], mode=mode),
                                               max_response_turns=run["maxResponseTurns"], client=client)
        await context.capture_screenshot(session, "desktop-final")
        await context.complete_run(notes=result["notes"])
    finally:
        await teardown(close_resources(python, session, client))
