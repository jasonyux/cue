"""HTTP routes for continuous sessions, registered on the runner's FastAPI app under /api/live/*."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ..errors import RunnerCoreError
from .actor.cmux.approvals import answer as answer_cmux_approval
from .actor.prompts import live_context_text
from .actor.standalone.approvals import WAITING as CLAUDE_WAITING
from .actor.standalone.approvals import answer as answer_claude_approval
from .actor.standalone.claude import models as claude_models
from .actor.standalone.codex import models as codex_models
from .config import LiveSettings
from .health import check_settings
from .session import LiveSessionManager


async def feedback_body(request: Request) -> tuple[str, str, int | None]:
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("proposalsTs"), str) or not isinstance(body.get("decision"), str):
        raise RunnerCoreError("Feedback needs proposalsTs and decision.", code="invalid_request", status_code=400)
    choice = body.get("choice")
    if choice is not None and (isinstance(choice, bool) or not isinstance(choice, int)):
        raise RunnerCoreError("choice must be an integer index into the proposal_set's proposals.", code="invalid_request",
                              status_code=400)
    return body["proposalsTs"], body["decision"], choice


async def summary_feedback_body(request: Request) -> tuple[str, str, str]:
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("summaryTs"), str):
        raise RunnerCoreError("Summary feedback needs summaryTs.", code="invalid_request", status_code=400)
    comment = body.get("comment", "")
    verdict = body.get("verdict", "wrong")
    if not isinstance(comment, str) or not isinstance(verdict, str):
        raise RunnerCoreError("comment and verdict must be strings.", code="invalid_request", status_code=400)
    return body["summaryTs"], comment, verdict


async def reason_body(request: Request) -> tuple[str, str, bool]:
    """A reject reason: the proposal set, the reason, and whether the person typed it (the console's "other…")
    rather than clicked a preset."""
    body = await request.json()
    if (not isinstance(body, dict) or not isinstance(body.get("proposalsTs"), str) or not isinstance(body.get("reason"), str)
            or not isinstance(body.get("typed"), bool)):
        raise RunnerCoreError("A reject reason needs proposalsTs, reason and typed.", code="invalid_request", status_code=400)
    return body["proposalsTs"], body["reason"], body["typed"]


async def idle_trigger_body(request: Request) -> tuple[bool, float]:
    """The idle trigger's switch from the console's menu: on or off, and the pause in seconds."""
    body = await request.json()
    idle_s = body.get("idleS") if isinstance(body, dict) else None
    if (not isinstance(body, dict) or not isinstance(body.get("enabled"), bool) or isinstance(idle_s, bool)
            or not isinstance(idle_s, int | float)):
        raise RunnerCoreError("The idle trigger needs enabled (true or false) and idleS (seconds).", code="invalid_request", status_code=400)
    return body["enabled"], float(idle_s)


async def task_target(request: Request) -> tuple[dict[str, Any], str, int | None]:
    """The body of a task flag request: the task, and the entry number or None for the task itself."""
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("taskId"), str):
        raise RunnerCoreError("Task feedback needs taskId.", code="invalid_request", status_code=400)
    step = body.get("step")
    if step is not None and (isinstance(step, bool) or not isinstance(step, int)):
        raise RunnerCoreError("step must be an entry number, or left out for the task itself.", code="invalid_request", status_code=400)
    return body, body["taskId"], step


async def settings_body(request: Request) -> dict[str, Any]:
    """A request's settings: a JSON object of camelCase settings, or nothing."""
    body = await request.body()
    values = await request.json() if body.strip() else {}
    if not isinstance(values, dict):
        raise RunnerCoreError("Live session settings must be a JSON object.", code="invalid_request", status_code=400)
    return values


MODEL_LISTS = {"claude": claude_models, "codex": codex_models}  # the agents whose models the console offers, each listed by its own SDK
MODELS: dict[str, list[dict[str, Any]]] = {}  # agent → its models, asked once and kept until the backend restarts


def register(app: FastAPI, live: LiveSessionManager, *, default_model: str = "") -> None:
    @app.get("/api/live/session")
    async def live_status() -> dict[str, Any]:
        return live.status()

    @app.post("/api/live/session", status_code=202)
    async def live_start(request: Request) -> dict[str, Any]:
        return await live.start(await settings_body(request))

    @app.post("/api/live/settings/check")
    async def live_settings_check(request: Request) -> dict[str, Any]:
        """Probe the model endpoints and the observer's permissions for the settings the form holds, before starting."""
        try:
            settings = live.defaults.with_overrides(await settings_body(request))
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        return await check_settings(settings, default_model=default_model)

    @app.post("/api/live/settings")
    async def live_settings_save(request: Request) -> dict[str, Any]:
        """Write the settings page's changes (a provider's key or base URL to .env, the rest to settings.yaml), then read both
        again as the next session's defaults; nothing to write only reads them again, for a file edited by hand. A running
        session keeps its own."""
        values = await settings_body(request)
        try:
            live.defaults.save(values)
            live.defaults = LiveSettings.load()
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        return live.status()

    @app.post("/api/live/session/stop")
    async def live_stop() -> dict[str, Any]:
        return await live.stop()

    @app.post("/api/live/session/pause")
    async def live_pause() -> dict[str, Any]:
        return live.pause()

    @app.post("/api/live/session/resume")
    async def live_resume() -> dict[str, Any]:
        return live.resume()

    @app.post("/api/live/session/propose")
    async def live_propose() -> dict[str, Any]:
        return await live.propose_now()

    @app.post("/api/live/session/idle-trigger")
    async def live_idle_trigger(request: Request) -> dict[str, Any]:
        """On or off and the pause: the running session at once, and saved as the next sessions' default."""
        enabled, idle_s = await idle_trigger_body(request)
        return live.set_idle_trigger(enabled, idle_s)

    @app.post("/api/live/session/tune")
    async def live_tune(request: Request) -> dict[str, Any]:
        """The landing page's sliders: the running session at once, and saved as the next sessions' default."""
        body = await request.json()
        if not isinstance(body, dict):
            raise RunnerCoreError("Tuning needs an object of settings.", code="invalid_request", status_code=400)
        return live.tune(body)

    @app.get("/api/live/session/summaries")
    async def live_summaries(limit: int = 50) -> list[dict[str, Any]]:
        return live.summaries(max(1, min(limit, 500)))

    @app.get("/api/live/session/proposals")
    async def live_proposals(limit: int = 50) -> list[dict[str, Any]]:
        return live.proposals(max(1, min(limit, 500)))

    @app.get("/api/live/history")
    async def live_history(q: str = "", regex: str | None = None, kinds: str = "", since: str | None = None, until: str | None = None,
                           limit: int = 20) -> dict[str, Any]:
        """The history index as the search tool reads it: keywords (all must match, by stem; a word may end in *), a regex,
        some kinds (comma-separated), a time slice."""
        try:
            hits = live.index.search(q, regex=regex, kinds=[kind for kind in kinds.split(",") if kind], since=since, until=until, limit=limit)
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        return {"hits": [hit.wire() for hit in hits]}

    @app.post("/api/live/session/proposals/feedback")
    async def live_feedback(request: Request) -> dict[str, Any]:
        proposals_ts, decision, choice = await feedback_body(request)
        return await live.record_feedback(proposals_ts, decision, choice=choice)

    @app.post("/api/live/session/proposals/feedback/reason")
    async def live_feedback_reason(request: Request) -> dict[str, Any]:
        proposals_ts, reason, typed = await reason_body(request)
        return live.record_reject_reason(proposals_ts, reason, typed)

    @app.post("/api/live/session/summaries/feedback")
    async def live_summary_feedback(request: Request) -> dict[str, Any]:
        summary_ts, comment, verdict = await summary_feedback_body(request)
        return live.record_summary_feedback(summary_ts, comment, verdict)

    @app.get("/api/live/agents/{agent}/models")
    async def live_agent_models(agent: str) -> dict[str, Any]:
        """The models an agent offers the signed-in user, as its own SDK lists them, for the chat's model picker:
        {models: [{id, label, detail, default}]}. Asked of the agent once; the backend keeps the list until it restarts."""
        if agent not in MODEL_LISTS:
            raise RunnerCoreError(f"No model list for the {agent} agent.", code="invalid_request", status_code=404)
        if agent not in MODELS:
            MODELS[agent] = await MODEL_LISTS[agent]()
        return {"models": MODELS[agent]}

    @app.post("/api/live/session/runs/{run_id}/approval")
    async def live_run_approval(run_id: str, request: Request) -> dict[str, Any]:
        """Answer the permission request a run's Claude is waiting on: {"mode": "once" | "deny"}. A claude run keeps its
        requests itself; a cmux run's are in cmux's Feed."""
        body = await request.json()
        mode = body.get("mode") if isinstance(body, dict) else None
        if run_id in CLAUDE_WAITING:
            await answer_claude_approval(run_id, mode)
        else:
            await answer_cmux_approval(run_id, mode)
        return {"runId": run_id, "mode": mode}

    @app.get("/api/live/session/browser")
    async def live_browser() -> dict[str, Any]:
        """The agent browser's tabs, for the monitor's zoomed view."""
        return live.agent_browser().wire()

    @app.get("/api/live/session/browser/stream")
    async def live_browser_stream(tab: str | None = None, width: int = 1280) -> StreamingResponse:
        """A tab as video: JPEG frames as they change (multipart, what an <img> plays). Without ``tab`` it follows the
        agent. The screencast runs only while this response is read."""
        browser = live.agent_browser()

        async def body():
            yield b"--frame\r\n"
            async for frame in browser.frames(tab, max(160, min(width, 1280))):
                # The boundary ending the part comes right after the frame: a browser shows a part only once it has ended.
                yield b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n--frame\r\n"
        return StreamingResponse(body(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-cache, no-transform"})

    @app.post("/api/live/session/browser/input")
    async def live_browser_input(request: Request) -> dict[str, Any]:
        """One pointer or keyboard event from the zoomed monitor: {tab?, kind: move|down|up|wheel|key|text, …}."""
        event = await request.json()
        await live.agent_browser().input(event.get("tab"), event)
        return {"ok": True}

    @app.post("/api/live/session/browser/tabs")
    async def live_browser_tabs(request: Request) -> dict[str, Any]:
        """Open a tab ({action: "new", url?}), close one ({action: "close", tab}), or go back or forward in one ({action: "back" | "forward", tab?})."""
        body = await request.json()
        browser = live.agent_browser()
        if body.get("action") == "new":
            return await browser.new_tab(body.get("url"))
        if body.get("action") == "close":
            await browser.close_tab(body["tab"])
            return {"closed": body["tab"]}
        if body.get("action") in ("back", "forward"):
            await browser.go(body.get("tab"), body["action"])
            return {"went": body["action"]}
        raise RunnerCoreError("action must be new, close, back or forward.", code="invalid_request", status_code=400)

    @app.get("/api/live/session/remote/stream")
    async def live_remote_stream(width: int = 1280) -> StreamingResponse:
        """The remote desktop as video, between runs: screenshots a few times a second (multipart JPEG, what an <img>
        plays). Refused, as an error rather than a stream, while a run holds the desktop."""
        view = live.remote_view()
        await view.connect()

        async def body():
            yield b"--frame\r\n"
            async for frame in view.frames(max(160, min(width, 1920))):
                yield b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n--frame\r\n"
        return StreamingResponse(body(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-cache, no-transform"})

    @app.post("/api/live/session/remote/input")
    async def live_remote_input(request: Request) -> dict[str, Any]:
        """One pointer or keyboard event from the monitor, run on the remote desktop through PyAutoGUI."""
        await live.remote_view().input(await request.json())
        return {"ok": True}

    @app.get("/api/live/session/chat")
    async def live_chat(id: str | None = None) -> dict[str, Any]:
        """The chat with the agent: the current conversation (or the earlier one ``id`` names), its messages, the
        worklog lines the next message will carry, and every conversation; with no session running, to read."""
        return live.chat(id)

    @app.get("/api/live/sessions/{session_id}/chat")
    async def live_session_chat(session_id: str, id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(live.stored_chat, session_id, id)

    @app.get("/api/live/chats")
    async def live_chats() -> dict[str, Any]:
        """Every conversation with the agent, across sessions, latest first."""
        return live.chats_list()

    @app.patch("/api/live/chats/{chat_id}")
    async def live_chat_mark(chat_id: str, request: Request) -> dict[str, Any]:
        """A display name for a conversation in place of its first message (null or empty takes it away), or its pin."""
        body = await request.json()
        given = body if isinstance(body, dict) else {}
        if (given.get("title") is not None and not isinstance(given["title"], str)) or not isinstance(given.get("pinned", False), bool):
            raise RunnerCoreError("title must be a string or null, pinned a boolean.", code="invalid_request", status_code=400)
        marks: dict[str, Any] = {}
        if "title" in given:
            marks["title"] = (given["title"] or "").strip()[:80] or None
        if "pinned" in given:
            marks["pinned"] = given["pinned"]
        return live.mark_chat(chat_id, **marks)

    @app.post("/api/live/session/chat/reset")
    async def live_chat_reset() -> dict[str, Any]:
        return live.chat_reset()

    @app.post("/api/live/session/chat/switch")
    async def live_chat_switch(request: Request) -> dict[str, Any]:
        """Continue an earlier conversation of the session: it becomes the one the next message goes to."""
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise RunnerCoreError("Switching needs the conversation's id.", code="invalid_request", status_code=400)
        return live.chat_switch(body["id"])

    @app.get("/api/live/session/context")
    async def live_context() -> dict[str, Any]:
        inputs = live.actor_inputs()
        return {"text": live_context_text(inputs) if inputs is not None else None}

    @app.get("/api/live/session/screenshots")
    async def live_screenshots(limit: int = 200) -> list[dict[str, Any]]:
        """The screenshots the observer stored, oldest first, each fetched by name from the route below."""
        return live.screenshots(limit=max(1, min(limit, 2000)))

    @app.get("/api/live/session/changes")
    async def live_changes(limit: int = 50) -> list[dict[str, Any]]:
        """The belief updates that changed the task list, oldest first, with their ops."""
        return live.task_changes(limit=max(1, min(limit, 500)))

    @app.post("/api/live/session/permissions")
    async def live_answer_permission(request: Request) -> dict[str, Any]:
        """An answer to a permission question: {"app": "Slack", "level": "block" | "look" | "use" | "deny", "scope"?: "session" | "once" | "task"}."""
        body = await request.json()
        if not isinstance(body, dict):
            raise RunnerCoreError("Body needs app and level.", code="invalid_request", status_code=400)
        return await asyncio.to_thread(live.answer_permission, str(body.get("app") or ""), str(body.get("level") or ""), str(body.get("scope") or ""))

    @app.get("/api/live/session/screen/blocked")
    async def live_blocked_glimpse() -> Response:
        """The frontmost window while the latest look is blocked, blurred past reading: for the monitor, taken now, kept nowhere."""
        return Response(await asyncio.to_thread(live.blocked_glimpse), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/live/session/screenshots/{name}")
    async def live_screenshot(name: str) -> Response:
        return Response(await asyncio.to_thread(live.screenshot_png, name), media_type="image/png")

    # Stored sessions: the read-only history view. Feedback is allowed there too, as labeling.

    @app.get("/api/live/sessions")
    async def live_previous(limit: int = 20, offset: int = 0, named: bool | None = None) -> list[dict[str, Any]]:
        """A page of the stored sessions, newest first; ``named`` keeps only the sessions given a name (true) or not (false)."""
        return await asyncio.to_thread(live.list_sessions, max(1, min(limit, 100)), max(0, offset), named)

    @app.get("/api/live/usage")
    async def live_usage(days: float = 3) -> dict[str, Any]:
        """Tokens and cost per model and module: every stored session, the last ``days`` days, and the running session."""
        return await asyncio.to_thread(live.usage_report, days)

    @app.get("/api/live/disk")
    async def live_disk() -> dict[str, Any]:
        """The data folder's bytes: each session by what fills it, the rest by what it is, and what its volume has free."""
        return await asyncio.to_thread(live.disk_report)

    @app.get("/api/live/sessions/{session_id}")
    async def live_session_detail(session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(live.session_detail, session_id)

    @app.patch("/api/live/sessions/{session_id}")
    async def live_session_rename(session_id: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        name = body.get("name") if isinstance(body, dict) else None
        if name is not None and not isinstance(name, str):
            raise RunnerCoreError("name must be a string or null.", code="invalid_request", status_code=400)
        return await asyncio.to_thread(live.rename_session, session_id, (name or "").strip()[:80] or None)

    @app.delete("/api/live/sessions/{session_id}")
    async def live_session_delete(session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(live.delete_session, session_id)

    @app.post("/api/live/disk/{action}", status_code=202)
    async def live_disk_pack(action: Literal["compress", "restore"], request: Request) -> dict[str, Any]:
        """Start packing stored sessions' screenshots and model-call logs in place, still readable, or writing compressed
        ones back as they were, a few at a time (storage/pack.py): {"ids": [...]}. Answers with the disk report, whose
        ``packing`` says how far along they are."""
        body = await request.json()
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise RunnerCoreError("Body needs ids, the sessions.", code="invalid_request", status_code=400)
        return await asyncio.to_thread(live.pack_sessions, ids, restoring=action == "restore")

    @app.post("/api/live/tasks")
    async def live_edit_tasks(request: Request) -> dict[str, Any]:
        """Operator corrections to the task list: {"ops": [{"op": "update", "taskId", "done": [n]}, {"op": "done", "taskId"}]}."""
        body = await request.json()
        ops = body.get("ops") if isinstance(body, dict) else None
        if not isinstance(ops, list):
            raise RunnerCoreError("Body needs an ops list.", code="invalid_request", status_code=400)
        return await asyncio.to_thread(live.edit_tasks, ops)

    @app.post("/api/live/tasks/feedback")
    async def live_task_flag(request: Request) -> dict[str, Any]:
        body, task_id, step = await task_target(request)
        if not isinstance(body.get("verdict"), str):
            raise RunnerCoreError("Task feedback needs a verdict, up or down.", code="invalid_request", status_code=400)
        return live.flag_task(task_id, step, body["verdict"])  # on the loop: a quick write to memory and the stream

    @app.post("/api/live/tasks/feedback/reason")
    async def live_task_flag_reason(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("flagId"), str) or not isinstance(body.get("reason"), str):
            raise RunnerCoreError("A reason needs flagId and reason.", code="invalid_request", status_code=400)
        return live.flag_task_reason(body["flagId"], body["reason"])  # on the loop, as above

    @app.delete("/api/live/tasks/feedback/{flag_id}")
    async def live_task_unflag(flag_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(live.unflag_task, flag_id)

    @app.post("/api/live/sessions/{session_id}/proposals/feedback")
    async def live_session_feedback(session_id: str, request: Request) -> dict[str, Any]:
        proposals_ts, decision, choice = await feedback_body(request)
        return await live.record_feedback(proposals_ts, decision, session_id, choice)

    @app.post("/api/live/sessions/{session_id}/proposals/feedback/reason")
    async def live_session_feedback_reason(session_id: str, request: Request) -> dict[str, Any]:
        proposals_ts, reason, typed = await reason_body(request)
        return live.record_reject_reason(proposals_ts, reason, typed, session_id)

    @app.post("/api/live/sessions/{session_id}/summaries/feedback")
    async def live_session_summary_feedback(session_id: str, request: Request) -> dict[str, Any]:
        summary_ts, comment, verdict = await summary_feedback_body(request)
        return live.record_summary_feedback(summary_ts, comment, verdict, session_id)

    @app.get("/api/live/sessions/{session_id}/screenshots")
    async def live_session_screenshots(session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await asyncio.to_thread(live.screenshots, session_id, limit=max(1, min(limit, 2000)))

    @app.get("/api/live/sessions/{session_id}/changes")
    async def live_session_changes(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(live.task_changes, session_id, limit=max(1, min(limit, 500)))

    @app.get("/api/live/sessions/{session_id}/usage")
    async def live_session_usage(session_id: str) -> list[dict[str, Any]]:
        """The cost rows of one stored session, like one entry of /api/live/usage."""
        return await asyncio.to_thread(live.stored_usage, session_id)

    @app.get("/api/live/sessions/{session_id}/screenshots/{name}")
    async def live_session_screenshot(session_id: str, name: str) -> Response:
        return Response(await asyncio.to_thread(live.screenshot_png, name, session_id), media_type="image/png")
