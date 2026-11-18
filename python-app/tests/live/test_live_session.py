"""Session manager: lifecycle, stored sessions, the time limit, and the HTTP surface."""

import asyncio
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from live_helpers import make_manager
from PIL import Image

from app.api import create_app
from app.errors import RunnerCoreError
from app.live import session as session_module
from app.live.config import LiveSettings, ModelEndpoint
from app.live.observer.recorder import record as record_row
from app.live.session import LiveSessionManager
from app.live.storage.paths import session_paths
from app.live.utils.jsonl import append_jsonl, read_jsonl, tail_jsonl
from app.live.utils.time import seconds_between, timestamp
from app.runner import RunnerManager


async def test_session_manager_lifecycle_and_context(tmp_path):
    ranked = ('{"summary": "Budget", "proposals": ['
              '{"kind": "act", "action": "propose", "task": "Total the budget sheet", "reason": "r", "confidence": 4}, '
              '{"kind": "explain", "action": "do", "task": "Explain the SUMIF error", "reason": "e", "confidence": 3}]}')
    # propose_now runs the belief updater between the summarizer and the proposer, so it takes a reply too.
    manager, client, observers = make_manager(tmp_path, ['{"summary": "Edited the budget sheet."}', "Nothing new.", ranked])
    assert manager.status()["active"] is False and manager.actor_inputs() is None
    status = await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})
    session = status["session"]
    assert status["active"] and Path(session["folder"]).is_dir() and observers[0].alive()
    assert session["settings"]["proposerIntervalS"] == 3600 and json.loads((Path(session["folder"]) / "session.json").read_text())["id"] == session["id"]
    with pytest.raises(Exception, match="already active"):
        await manager.start()
    unset = LiveSessionManager(data_root=tmp_path / "unset", settings=LiveSettings(summary=ModelEndpoint(model="qwen"), belief=ModelEndpoint(model="qwen"), proposer=ModelEndpoint()),
                               client_factory=lambda endpoint: client, observer_factory=manager.observer_factory)
    with pytest.raises(Exception, match="proposer model is not set"):  # no silent fallback to the task agent's model
        await unset.start()
    paths = session_paths(tmp_path, session["id"])
    record_row(paths, manager.memory.stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Numbers", "window": "Budget", "tree": "[0] AXWindow"})
    result = await manager.propose_now()
    assert result["summary"]["summary"] == "Edited the budget sheet." and result["proposals"]["action"] == "propose"
    assert result["proposals"]["effect"] == "proposed"
    given = manager.actor_inputs()
    assert [item.summary for item in given["worklog"]] == ["Edited the budget sheet."] and given["proposal"].task == "Total the budget sheet"
    assert manager.run_directory("run-1") == paths.runs_dir / "run-1"
    manager.record_run("run-1", "ctx", prompt="Total it")
    assert tail_jsonl(paths.runs, 1)[0]["runId"] == "run-1" and manager.status()["session"]["runs"] == ["run-1"]
    assert manager.session.runs.active == {"run-1": "Total it"}
    manager.record_run_finished("run-1", "completed")
    manager.record_run_finished("unknown", "failed")  # not a run of this session: ignored
    assert manager.session.runs.active == {}
    assert [(item["event"], item.get("status")) for item in tail_jsonl(paths.runs, 5)] == [("started", None), ("finished", "completed")]
    assert manager.status()["session"]["summaryTicks"] == 1 and manager.status()["session"]["snapshotCount"] == 1
    proposals_ts = result["proposals"]["ts"]
    with pytest.raises(Exception, match="accept"):
        await manager.record_feedback(proposals_ts, "maybe")
    with pytest.raises(Exception, match="Unknown proposal_set"):
        await manager.record_feedback("2000-01-01T00:00:00Z", "accept")
    with pytest.raises(Exception, match="choice must be"):
        await manager.record_feedback(proposals_ts, "accept", choice=2)
    record = await manager.record_feedback(proposals_ts, "reject")
    assert record["decision"] == "reject" and record["choice"] is None and record["task"] == "Total the budget sheet"
    # The decision is written at once, without its context; the digest of what the proposer saw lands in the background.
    assert record["context"] == "" and manager.proposals()[-1]["feedback"]["digested"] is False and manager.digests
    await manager.wait_for_digests()
    digested = tail_jsonl(paths.feedback, 1)[0]
    assert digested["context"] and digested["model"] == "fake-model" and digested["usage"]["input_tokens"] == 100
    assert manager.proposals()[-1]["feedback"]["digested"] is True and read_jsonl(manager.memory.feedback)[0][-1]["context"] == digested["context"]
    # The digest is the last call: the preference updater reads the decision on its timer, or at the stop.
    assert tail_jsonl(paths.model_calls, 1)[0]["kind"] == "proposer_feedback" and "Numbers" in client.calls[-1]["messages"][1]["content"]
    assert read_jsonl(paths.preferences)[0] == [] and not manager.memory.preferences_file.exists()
    with pytest.raises(Exception, match="must not be empty"):
        manager.record_reject_reason(proposals_ts, "   ", False)
    reasoned = manager.record_reject_reason(proposals_ts, "I am on it already", True)  # a reason after the fact, typed, written onto the decision
    assert reasoned["rejectReason"] == "I am on it already" and reasoned["reasonTyped"] is True and reasoned["ts"] == record["ts"] and reasoned["context"] == digested["context"]
    assert manager.proposals()[-1]["feedback"]["rejectReason"] == "I am on it already"
    assert {k: read_jsonl(manager.memory.feedback)[0][-1][k] for k in ("rejectReason", "reasonTyped")} == {"rejectReason": "I am on it already", "reasonTyped": True}
    assert manager.memory.stream.events()[-1].data == {"proposalsTs": proposals_ts, "decisionTs": record["ts"], "reason": "I am on it already", "typed": True}
    kinds = [event.kind for event in manager.memory.stream.events(session_id=session["id"])]  # the same story, in one stream
    assert kinds == ["screen_changed", "summary_written", "belief_updated", "proposals_made", "run_started", "run_finished", "proposal_rejected", "decision_digested", "reject_reason_given"]
    assert [item["task"] for item in record["proposals"]] == ["Total the budget sheet", "Explain the SUMIF error"]
    await manager.record_feedback(proposals_ts, "accept", choice=1)  # the latest decision wins; second choice picked
    with pytest.raises(Exception, match="not a rejection"):
        manager.record_reject_reason(proposals_ts, "not now", False)
    annotated = manager.proposals()[-1]
    assert annotated["feedback"] == {"id": annotated["feedback"]["id"], "decision": "accept", "ts": annotated["feedback"]["ts"], "choice": 1, "digested": False}  # digest still in flight
    assert len(annotated["feedback"]["id"]) == 12
    assert manager.status()["session"]["latestProposals"]["feedback"]["decision"] == "accept"
    assert manager.proposal_sent(proposals_ts, "Explain the  SUMIF error") is None  # sent to the chat as proposed: nothing to add
    sent = manager.proposal_sent(proposals_ts, "Explain the SUMIF error in B4, briefly")  # changed first: the acceptance keeps what was sent
    assert sent["modified"] is True and sent["sentText"] == "Explain the SUMIF error in B4, briefly" and sent["ts"] == annotated["feedback"]["ts"]
    assert manager.proposals()[-1]["feedback"]["modified"] is True
    assert [(item["decision"], item["choice"], item["kind"], item["task"]) for item in tail_jsonl(paths.feedback, 10)] == [
        ("reject", None, "act", "Total the budget sheet"), ("accept", 1, "explain", "Explain the SUMIF error")]
    assert (await manager.record_feedback(proposals_ts, "accept"))["choice"] == 0  # accept without a choice takes the top proposal
    call_id = result["proposals"]["callId"]  # the proposer call that made the set: the decisions carry it
    assert len(call_id) == 12 and [(item["decision"], item["task"], item["callId"]) for item in read_jsonl(manager.memory.feedback)[0]][-2:] == [
        ("accept", "Explain the SUMIF error", call_id), ("accept", "Total the budget sheet", call_id)]
    summary_ts = result["summary"]["ts"]
    with pytest.raises(Exception, match="verdict"):
        manager.record_summary_feedback(summary_ts, "x", "maybe")
    with pytest.raises(Exception, match="Unknown summary"):
        manager.record_summary_feedback("2000-01-01T00:00:00Z", "x")
    flag = manager.record_summary_feedback(summary_ts, "  I was in the ICLR paper, not ConFit.  ")
    assert flag["verdict"] == "wrong" and flag["comment"] == "I was in the ICLR paper, not ConFit." and flag["summary"] == "Edited the budget sheet."
    assert flag["windowEnd"] == summary_ts and flag["model"] == "fake-model"
    flagged = manager.summaries()[-1]
    assert flagged["feedback"]["verdict"] == "wrong" and flagged["feedback"]["comment"].startswith("I was in")
    manager.record_summary_feedback(summary_ts, "", "ok")  # the latest flag wins
    assert manager.memory.stream.events(kinds=("summary_flagged",))[-1].data["verdict"] == "ok"
    assert manager.summaries()[-1]["feedback"]["verdict"] == "ok"
    stopped = await manager.stop()
    assert stopped["active"] is False and stopped["stopped"]["stoppedAt"] and not observers[0].alive() and client.closed
    assert len(read_jsonl(paths.preferences)[0]) == 1 and manager.memory.stream.events(kinds=("preferences_updated",))  # the stop's final tick read the decisions
    listed = LiveSessionManager.previous_sessions(tmp_path)[0]
    assert listed["id"] == session["id"] and listed["stopReason"] == "stopped by operator" and listed["proposalCount"] == 1
    with pytest.raises(Exception, match="No live session"):
        await manager.stop()
    detail = manager.session_detail(session["id"])
    assert detail["session"]["active"] is False and detail["proposals"][-1]["feedback"]["decision"] == "accept"
    assert detail["summaries"][-1]["summary"] == "Edited the budget sheet." and detail["session"]["snapshotCount"] == 1
    await manager.record_feedback(proposals_ts, "reject", session["id"])
    await manager.wait_for_digests()  # for a stored session the digest uses a client on the default proposer endpoint
    labeled = manager.session_detail(session["id"])["proposals"][-1]["feedback"]
    assert labeled["decision"] == "reject" and labeled["digested"] is True
    manager.record_summary_feedback(summary_ts, "stored flag", "wrong", session["id"])
    assert manager.session_detail(session["id"])["summaries"][-1]["feedback"]["comment"] == "stored flag"
    for bad in ("../etc", "nope"):
        with pytest.raises(Exception):  # noqa: B017 - invalid ids and unknown ids both refuse
            manager.session_detail(bad)


async def test_stored_sessions_from_older_versions_are_backfilled(tmp_path):
    manager, _client, _observers = make_manager(tmp_path)
    folder = tmp_path / "sessions" / "20260917-211843"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({
        "id": "20260917-211843", "folder": str(folder), "startedAt": "2026-09-18T01:18:43Z", "stoppedAt": None,
        "settings": {"model": "qwen3.8-27b", "baseUrl": "http://vllm/v1", "snapshotIntervalS": 10.0, "proposerIntervalS": 300.0,
                     "summaryThinking": False, "proposerThinking": True, "allowlist": [], "maxElements": 400,
                     "screenshotsOnSwitch": True, "notify": True, "summaryHistoryMinutes": 60},
        "usage": None, "runs": [], "lastError": None}), encoding="utf-8")
    (folder / "proposals.jsonl").write_text(
        '{"ts": "2026-09-18T01:20:00Z", "action": "none"}\n'
        '{"ts": "2026-09-18T01:25:00Z", "action": "propose", "task": "Total the sheet", "reason": "r", "confidence": 3}\n',
        encoding="utf-8")
    (folder / "live_model_calls.jsonl").write_text(  # what the proposer saw that round: the digest at decision time reads it
        '{"ts": "2026-09-18T01:25:00Z", "kind": "proposer", "windowEnd": "2026-09-18T01:25:00Z", '
        '"request": {"messages": [{"role": "user", "content": "Numbers — Budget"}]}, "response": {}}\n', encoding="utf-8")
    listed = manager.list_sessions()
    assert [item["id"] for item in listed] == ["20260917-211843"]
    settings = listed[0]["settings"]
    assert settings["summaryModel"] == "qwen3.8-27b" and settings["proposerBaseUrl"] == "http://vllm/v1"
    assert settings["summaryScreenshots"] is False and settings["screenshotMode"] == "switch"
    assert settings["effectiveScreenshotMode"] == "switch" and settings["maxSessionMinutes"] == 120
    assert "screenshotsOnSwitch" not in settings and listed[0]["proposalCount"] == 2 and listed[0]["stopReason"] is None
    assert settings["maxProposals"] == 3
    detail = manager.session_detail("20260917-211843")
    assert detail["session"]["settings"]["proposerModel"] == "qwen3.8-27b" and detail["proposals"][0]["action"] == "none"
    assert detail["proposals"][0]["proposals"] == [] and detail["proposals"][1]["proposals"] == [
        {"kind": "act", "task": "Total the sheet", "reason": "r", "confidence": 3, "evidence": []}]
    record = await manager.record_feedback("2026-09-18T01:25:00Z", "accept", "20260917-211843")
    assert record["choice"] == 0 and record["kind"] == "act" and record["task"] == "Total the sheet"
    await manager.wait_for_digests()
    assert manager.session_detail("20260917-211843")["proposals"][-1]["feedback"]["digested"] is True


async def test_session_stops_itself_at_the_time_limit(tmp_path):
    manager, _client, observers = make_manager(tmp_path)
    status = await manager.start({"maxSessionMinutes": 0.005, "proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})
    assert status["session"]["expiresAt"]
    await asyncio.sleep(0.8)
    assert manager.session is None and not observers[0].alive()
    stored = LiveSessionManager.previous_sessions(tmp_path)[0]
    assert stored["id"] == status["session"]["id"] and stored["stopReason"] == "max session time reached"
    unlimited = await manager.start({"maxSessionMinutes": 0, "proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})
    assert unlimited["session"]["expiresAt"] is None
    await manager.stop()


async def test_pausing_holds_the_loops_the_observer_and_new_runs(tmp_path):
    manager, client, _observers = make_manager(tmp_path, ['{"summary": "Edited the budget sheet."}'] * 5, summary_interval_s=0.05)
    status = await manager.start({"proposerIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})
    session = manager.session
    paths = session_paths(tmp_path, status["session"]["id"])
    record_row(paths, manager.memory.stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Numbers", "window": "Budget", "tree": "[0] AXWindow"})
    paused = manager.pause()
    assert paused["session"]["paused"] is True and paused["session"]["pausedAt"] and paths.pause_flag.exists()  # the observer reads the flag
    with pytest.raises(RunnerCoreError, match="already paused"):
        manager.pause()
    with pytest.raises(RunnerCoreError, match="paused"):
        await manager.propose_now()
    with pytest.raises(RunnerCoreError, match="paused"):
        manager.require_running()  # what the runner asks before a run
    await asyncio.sleep(0.3)
    assert session.summarizer.ticks == 0 and client.calls == []  # the loop woke several times and waited every time
    resumed = manager.resume()
    assert resumed["session"]["paused"] is False and resumed["session"]["pausedAt"] is None and not paths.pause_flag.exists()
    with pytest.raises(RunnerCoreError, match="not paused"):
        manager.resume()
    manager.require_running()
    await asyncio.sleep(0.3)
    assert session.summarizer.ticks >= 1  # the held tick ran on resume, over what was seen before the pause
    events = read_jsonl(manager.memory.stream.root / f"{timestamp()[:10]}.jsonl")[0]
    assert [item["kind"] for item in events if item["kind"].startswith("session_")] == ["session_paused", "session_resumed"]
    await manager.stop()


async def test_the_time_limit_counts_running_time_only(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "RESUME_GRACE_S", 0.0)  # the grace has its own test; here the limit is what resume leaves
    manager, _client, observers = make_manager(tmp_path)
    status = await manager.start({"maxSessionMinutes": 0.005, "proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})
    deadline = status["session"]["expiresAt"]
    paths = session_paths(tmp_path, status["session"]["id"])
    paused = manager.pause()
    await asyncio.sleep(1.0)  # well past the limit as first set
    assert manager.session is not None and manager.status()["session"]["expiresAt"] > paused["session"]["expiresAt"] >= deadline  # the deadline moves with the pause
    resumed = manager.resume()
    assert resumed["session"]["expiresAt"] > deadline and manager.session is not None
    await asyncio.sleep(1.5)  # the rest of the limit (0.3 s), counted from the resume
    assert manager.session is None and not observers[0].alive() and not paths.pause_flag.exists()
    stored = LiveSessionManager.previous_sessions(tmp_path)[0]
    assert stored["stopReason"] == "max session time reached" and stored["pausedAt"] is None and stored["pausedS"] == 1


async def test_a_session_resumed_near_its_limit_gets_a_minute(tmp_path):
    manager, _client, _observers = make_manager(tmp_path)
    await manager.start({"maxSessionMinutes": 0.02, "proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})  # 1.2 s
    manager.pause()
    await asyncio.sleep(0.2)
    resumed = manager.resume()  # about a second left: too close to the limit
    left = seconds_between(timestamp(), resumed["session"]["expiresAt"])
    assert 55 < left <= 60
    await asyncio.sleep(1.5)
    assert manager.session is not None  # the old deadline passed; the session runs on
    await manager.stop()


async def test_live_api_endpoints(tmp_path, monkeypatch):
    live, _client, _ = make_manager(tmp_path / "data", ['{"summary": "Browsing."}', '{"summary": "b", "action": "none"}'])
    template = tmp_path / "template"
    template.mkdir()
    scenarios = [{"id": "desktop-free-task", "labId": "desktop", "category": "productivity", "title": "Desktop",
                  "description": "d", "defaultPrompt": "p", "workspaceTemplatePath": str(template), "tags": ["desktop"]}]
    runner = RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=scenarios)
    app = create_app(manager=runner, live=live, acquire_lease=False)
    assert runner.live is live
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/session")).json()["active"] is False
        assert (await http.post("/api/live/session/stop")).status_code == 409
        assert (await http.post("/api/live/session/pause")).status_code == 409
        started = await http.post("/api/live/session", json={"notify": False, "proposerIntervalS": 3600, "summaryIntervalS": 3600, "actorAgents": ["desktop"]})
        assert started.status_code == 202 and started.json()["session"]["observerAlive"] is True and started.json()["session"]["paused"] is False
        assert (await http.post("/api/live/session/resume")).status_code == 409  # not paused
        paused = await http.post("/api/live/session/pause")
        assert paused.status_code == 200 and paused.json()["session"]["paused"] is True
        assert (await http.post("/api/live/session/pause")).status_code == 409
        held = await http.post("/api/live/session/chat/messages", json={"text": "hi", "agent": "desktop"})
        assert held.status_code == 409 and held.json()["code"] == "live_session_paused"
        assert (await http.post("/api/live/session/propose")).status_code == 409
        resumed = await http.post("/api/live/session/resume")
        assert resumed.status_code == 200 and resumed.json()["session"]["paused"] is False
        paths = session_paths(tmp_path / "data", started.json()["session"]["id"])
        record_row(paths, live.memory.stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Numbers", "window": "Budget",
                                               "tree": "[0] AXWindow", "visible": ["Slack — general"]})
        record_row(paths, live.memory.stream, {"kind": "screenshot_taken", "ts": "2026-09-17T10:00:05Z", "app": "Numbers", "window": "Budget", "path": "shot-1.png"})
        append_jsonl(paths.beliefs, {"ts": "2026-09-17T10:01:00Z", "windowStart": "2026-09-17T10:00:00Z", "windowEnd": "2026-09-17T10:01:00Z",
                                     "ops": [{"op": "add", "title": "Budget", "steps": [{"text": "Total the sheet"}]}], "tasks": [], "callId": "c1"})
        append_jsonl(paths.beliefs, {"ts": "2026-09-17T10:02:00Z", "ops": [], "tasks": []})  # nothing changed: not a change
        append_jsonl(paths.beliefs, {"ts": "2026-09-17T10:03:00Z", "ops": [], "tasks": [], "rounds": 2, "warned": ["Warning, not applied: close to entry 1"]})  # a held call: shown
        shown = (await http.get("/api/live/session")).json()["session"]
        assert shown["latestSnapshot"]["visible"] == ["Slack — general"] and shown["latestSnapshot"]["blocked"] is None
        assert (await http.get("/api/live/session/screen/blocked")).status_code == 409  # the glimpse exists only while a look is blocked
        # Permission questions: the apps looked at under ask with no answer yet, until an answer writes a rule (the file's, or the session's own).
        assert shown["permissionAsks"] == []
        (tmp_path / "settings.yaml").write_text("permissions:\n  default: ask\n")
        for app, bundle, ts in (("Slack", "com.tinyspeck.slackmacgap", "2026-09-17T10:00:06Z"), ("Notes", "com.apple.Notes", "2026-09-17T10:00:07Z")):
            record_row(paths, live.memory.stream, {"kind": "screen_changed", "ts": ts, "app": app, "bundle": bundle, "excluded": True, "reason": "ask"})
        asks = (await http.get("/api/live/session")).json()["session"]["permissionAsks"]
        assert asks == [{"app": "Notes", "bundle": "com.apple.Notes", "ts": "2026-09-17T10:00:07Z"}, {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap", "ts": "2026-09-17T10:00:06Z"}]
        assert (await http.post("/api/live/session/permissions", json={"app": "Slack", "level": "look"})).status_code == 200
        assert "Slack: look" in (tmp_path / "settings.yaml").read_text()  # a rule for good
        assert (await http.post("/api/live/session/permissions", json={"app": "Notes", "level": "look", "scope": "session"})).status_code == 200
        assert "Notes: look" in paths.settings.read_text() and "Notes" not in (tmp_path / "settings.yaml").read_text()  # this session only
        assert (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == []
        assert (await http.post("/api/live/session/permissions", json={"app": "Notes", "level": "maybe"})).status_code == 400
        # The agent's asks (ax.ask in its worker) come first, with the level and why, until a rule grants the level or a denial answers them.
        append_jsonl(paths.asks, {"ts": "2026-09-17T10:00:08Z", "app": "Mail", "bundle": "com.apple.mail", "level": "use", "why": "send the reply"})
        assert (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == [
            {"app": "Mail", "bundle": "com.apple.mail", "ts": "2026-09-17T10:00:08Z", "level": "use", "why": "send the reply", "source": "agent"}]
        assert (await http.post("/api/live/session/permissions", json={"app": "Mail", "level": "use", "scope": "session"})).status_code == 200  # Allow in this session
        assert "Mail: use" in paths.settings.read_text() and (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == []
        append_jsonl(paths.asks, {"ts": "2026-09-17T10:00:09Z", "app": "Finder", "bundle": "com.apple.finder", "level": "use", "why": "move the file"})
        assert (await http.post("/api/live/session/permissions", json={"app": "Finder", "level": "deny"})).status_code == 200  # Block: no rule, the ask answered
        assert (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == [] and "Finder" not in paths.settings.read_text()
        assert read_jsonl(paths.asks)[0][-1] == {"ts": read_jsonl(paths.asks)[0][-1]["ts"], "app": "Finder", "denied": True}
        append_jsonl(paths.asks, {"ts": "2026-09-17T10:00:10Z", "app": "Notes", "bundle": "com.apple.Notes", "level": "use", "why": "type the list"})
        assert (await http.post("/api/live/session/permissions", json={"app": "Notes", "level": "use", "scope": "once"})).status_code == 200  # Allow once: a row, no rule
        assert [(row["allowed"], row["scope"]) for row in read_jsonl(paths.asks)[0][-1:]] == [("use", "once")] and "Notes: use" not in paths.settings.read_text()
        assert (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == []
        append_jsonl(paths.asks, {"ts": "2026-09-17T10:00:11Z", "app": "Notes", "bundle": "com.apple.Notes", "level": "use", "why": "again"})
        assert (await http.post("/api/live/session/permissions", json={"app": "Notes", "level": "use", "scope": "task"})).status_code == 200  # Allow for this task: a row too
        assert read_jsonl(paths.asks)[0][-1]["scope"] == "task" and (await http.get("/api/live/session")).json()["session"]["permissionAsks"] == []
        record_row(paths, live.memory.stream, {"kind": "screen_unchanged", "ts": "2026-09-17T10:00:10Z", "app": "Slack", "excluded": True, "reason": "app", "pid": 24})
        assert (await http.get("/api/live/session")).json()["session"]["latestSnapshot"]["blocked"] == "app"  # for the monitor, on a repeated tick too
        monkeypatch.setattr("app.live.observer.sources.blurred_glimpse", lambda pid: Image.new("RGB", (pid, 16), "purple"))  # of the look's app
        glimpse = await http.get("/api/live/session/screen/blocked")
        assert glimpse.status_code == 200 and glimpse.headers["content-type"] == "image/jpeg" and glimpse.headers["cache-control"] == "no-store"
        assert Image.open(BytesIO(glimpse.content)).size == (24, 16) and not list(paths.screenshots.iterdir())[1:]  # shown, never stored
        assert shown["latestScreenshot"] == {"ts": "2026-09-17T10:00:05Z", "name": "shot-1.png", "app": "Numbers", "window": "Budget"}
        assert (await http.get("/api/live/session/screenshots")).json() == [shown["latestScreenshot"]]
        changes = (await http.get("/api/live/session/changes")).json()
        assert [(change["ts"], [op["op"] for op in change["ops"]], change["warned"], change["rounds"]) for change in changes] == [
            ("2026-09-17T10:01:00Z", ["add"], [], 1), ("2026-09-17T10:03:00Z", [], ["Warning, not applied: close to entry 1"], 2)]
        assert (changes[0]["ops"][0]["title"], changes[0]["callId"], changes[0]["rejected"]) == ("Budget", "c1", [])
        assert (await http.post("/api/live/session", json={})).status_code == 409
        assert (await http.post("/api/live/session", json={"bogus": 1})).status_code in (400, 409)
        assert (await http.get("/api/live/session/summaries")).json() == []
        proposed = await http.post("/api/live/session/propose")
        assert proposed.status_code == 200 and proposed.json()["proposals"]["action"] == "none"
        assert (await http.get("/api/live/session/context")).json()["text"].startswith("Observed context")
        usage = (await http.get("/api/live/usage?days=3")).json()
        assert usage["days"] == 3 and usage["session"] is not None and usage["all"] == usage["recent"] == usage["session"]
        proposed_ts = proposed.json()["proposals"]["ts"]
        assert (await http.post("/api/live/session/proposals/feedback", json={"proposalsTs": proposed_ts})).status_code == 400
        feedback = await http.post("/api/live/session/proposals/feedback", json={"proposalsTs": proposed_ts, "decision": "reject"})
        assert feedback.status_code == 200 and feedback.json()["decision"] == "reject"
        assert (await http.post("/api/live/session/proposals/feedback/reason", json={"proposalsTs": proposed_ts})).status_code == 400
        assert (await http.post("/api/live/session/proposals/feedback/reason", json={"proposalsTs": proposed_ts, "reason": "do it myself"})).status_code == 400  # typed missing
        reasoned = await http.post("/api/live/session/proposals/feedback/reason", json={"proposalsTs": proposed_ts, "reason": "do it myself", "typed": False})
        assert reasoned.status_code == 200 and reasoned.json()["rejectReason"] == "do it myself" and reasoned.json()["reasonTyped"] is False
        assert (await http.get("/api/live/session/proposals")).json()[-1]["feedback"]["decision"] == "reject"
        assert (await http.get("/api/live/session/screenshots/nope.png")).status_code == 404
        stopped = await http.post("/api/live/session/stop")
        assert stopped.status_code == 200 and stopped.json()["active"] is False
        listed = (await http.get("/api/live/sessions")).json()
        assert len(listed) == 1 and listed[0]["proposalCount"] == 1
        session_id = listed[0]["id"]
        detail = await http.get(f"/api/live/sessions/{session_id}")
        assert detail.status_code == 200 and detail.json()["session"]["active"] is False
        assert detail.json()["proposals"][-1]["feedback"]["decision"] == "reject"
        empty = await http.post(f"/api/live/sessions/{session_id}/proposals/feedback", json={"proposalsTs": proposed_ts, "decision": "accept"})
        assert empty.status_code == 400 and "no proposals" in empty.json()["error"]  # a "none" proposal set has nothing to accept
        assert (await http.post("/api/live/session/proposals/feedback", json={"proposalsTs": proposed_ts, "decision": "reject", "choice": "1"})).status_code in (400, 409)
        labeled = await http.post(f"/api/live/sessions/{session_id}/proposals/feedback", json={"proposalsTs": proposed_ts, "decision": "reject", "choice": None})
        assert labeled.status_code == 200 and labeled.json()["decision"] == "reject" and labeled.json()["choice"] is None
        summary_ts = proposed.json()["summary"]["ts"]
        assert (await http.post("/api/live/session/summaries/feedback", json={"comment": "x"})).status_code == 400
        flagged = await http.post(f"/api/live/sessions/{session_id}/summaries/feedback",
                                  json={"summaryTs": summary_ts, "comment": "wrong project", "verdict": "wrong"})
        assert flagged.status_code == 200 and flagged.json()["comment"] == "wrong project"
        assert (await http.get(f"/api/live/sessions/{session_id}")).json()["summaries"][-1]["feedback"]["verdict"] == "wrong"
        assert (await http.get("/api/live/sessions/does-not-exist")).status_code == 404
        assert (await http.get("/api/live/sessions/bad%2Fid")).status_code in (400, 404)
        assert (await http.get(f"/api/live/sessions/{session_id}/screenshots/nope.png")).status_code == 404
        assert [shot["name"] for shot in (await http.get(f"/api/live/sessions/{session_id}/screenshots")).json()] == ["shot-1.png"]
        assert [change["ts"] for change in (await http.get(f"/api/live/sessions/{session_id}/changes")).json()] == ["2026-09-17T10:01:00Z", "2026-09-17T10:03:00Z"]
        stored_usage = (await http.get(f"/api/live/sessions/{session_id}/usage")).json()
        assert stored_usage == (await http.get("/api/live/usage?days=3")).json()["all"] and stored_usage  # the only stored session
        assert (await http.get("/api/live/sessions/does-not-exist/usage")).status_code == 404
        renamed = await http.patch(f"/api/live/sessions/{session_id}", json={"name": "  Paper night  "})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Paper night"
        assert (await http.get("/api/live/sessions")).json()[0]["name"] == "Paper night"
        assert (await http.patch(f"/api/live/sessions/{session_id}", json={"name": 5})).status_code == 400
        assert (await http.patch("/api/live/sessions/does-not-exist", json={"name": "n"})).status_code == 404
        assert (await http.delete(f"/api/live/sessions/{session_id}")).json() == {"id": session_id, "deleted": True}
        assert (await http.get(f"/api/live/sessions/{session_id}")).status_code == 404
        edited = await http.post("/api/live/tasks", json={"ops": [{"op": "status", "taskId": "t999", "done": True}]})  # unknown id: no-op
        assert edited.status_code == 200 and edited.json() == {"tasks": [], "trash": []}
        assert (await http.post("/api/live/tasks", json={"ops": "nope"})).status_code == 400
        assert (await http.post("/api/live/tasks/feedback", json={"taskId": "t999", "verdict": "up"})).status_code == 404
        assert (await http.post("/api/live/tasks/feedback", json={"taskId": "t001", "step": "x", "verdict": "up"})).status_code == 400
        assert (await http.post("/api/live/tasks/feedback/reason", json={"flagId": "nope"})).status_code == 400
        assert (await http.post("/api/live/tasks/feedback/reason", json={"flagId": "nope", "reason": "made up"})).status_code == 404
        assert (await http.delete("/api/live/tasks/feedback/nope")).status_code == 404


def test_the_history_list_comes_a_page_at_a_time(tmp_path):
    root = tmp_path / "sessions"
    for day, name in (("20261001-090000", "paper night"), ("20261002-090000", None), ("20261003-090000", None)):
        (root / day).mkdir(parents=True)
        (root / day / "session.json").write_text(json.dumps({"id": day, "name": name}))
    pages = [[item["id"] for item in LiveSessionManager.previous_sessions(tmp_path, limit=2, offset=offset)] for offset in (0, 2, 4)]
    assert pages == [["20261003-090000", "20261002-090000"], ["20261001-090000"], []]
    assert [item["id"] for item in LiveSessionManager.previous_sessions(tmp_path, limit=2, named=True)] == ["20261001-090000"]  # a filter pages on its own
    assert [item["id"] for item in LiveSessionManager.previous_sessions(tmp_path, limit=2, offset=1, named=False)] == ["20261002-090000"]


async def test_rename_and_delete_are_for_stored_sessions_only(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data", ['{"summary": "s"}'])
    session_id = (await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False}))["session"]["id"]
    with pytest.raises(RunnerCoreError, match="Stop the session"):
        manager.rename_session(session_id, "paper night")
    with pytest.raises(RunnerCoreError, match="Stop the session"):
        manager.delete_session(session_id)
    await manager.stop()
    assert manager.rename_session(session_id, "paper night")["name"] == "paper night"
    assert manager.list_sessions()[0]["name"] == "paper night" and manager.session_detail(session_id)["session"]["name"] == "paper night"
    folder = tmp_path / "data" / "sessions" / session_id
    assert manager.delete_session(session_id) == {"id": session_id, "deleted": True} and not folder.exists()


async def test_stopping_session_is_still_the_running_one(tmp_path):
    """Teardown ends by writing session.json from the session object, which carries no name: a rename made while the
    final tick is still calling the model would be erased, so until then the session is not a stored one."""
    manager, client, _ = make_manager(tmp_path / "data", ['{"summary": "s"}'])
    session_id = (await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False}))["session"]["id"]
    record_row(session_paths(tmp_path / "data", session_id), manager.memory.stream,
               {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Numbers", "window": "Budget", "tree": "[0] AXWindow"})
    gate = asyncio.Event()
    complete = client.complete

    async def slow_complete(*args, **kwargs):
        await gate.wait()
        return await complete(*args, **kwargs)

    client.complete = slow_complete
    stopping = asyncio.create_task(manager.stop())
    await asyncio.sleep(0.05)  # the final summary tick is now waiting on the model
    status = manager.status()
    assert status["active"] and status["session"]["stopReason"] == "stopped by operator" and status["session"]["lastError"] is None
    with pytest.raises(RunnerCoreError, match="Stop the session"):
        manager.rename_session(session_id, "paper night")
    with pytest.raises(RunnerCoreError, match="No live session"):
        await manager.stop()
    gate.set()
    await stopping
    assert manager.session is None and manager.rename_session(session_id, "paper night")["name"] == "paper night"
    assert manager.list_sessions()[0]["name"] == "paper night"


async def test_a_failed_digest_leaves_the_decision_and_says_so(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data", ['{"summary": "s", "proposals": [{"kind": "act", "task": "Do it"}]}'])
    session_id = (await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False}))["session"]["id"]
    paths = session_paths(tmp_path / "data", session_id)
    append_jsonl(paths.proposals, {"ts": "2026-09-30T10:00:00Z", "action": "propose", "task": "Do it", "proposals": [{"kind": "act", "task": "Do it"}]})
    stored = await manager.record_feedback("2026-09-30T10:00:00Z", "accept")  # no logged proposer request for this round
    await manager.wait_for_digests()
    row = tail_jsonl(paths.feedback, 1)[0]
    assert row["ts"] == stored["ts"] and row["context"] == "" and row["digestError"].startswith("RunnerCoreError: No logged proposer request")
    assert manager.proposals()[-1]["feedback"]["digestError"] == row["digestError"] and read_jsonl(manager.memory.feedback)[0][-1]["task"] == "Do it"
    await manager.stop()


async def test_desktop_remote_follows_the_source_setting(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data")
    assert manager.desktop_source() == ("local",) and manager.desktop_remote() is None
    manager.defaults = manager.defaults.with_overrides({"actorDesktopSource": "local,remote", "desktopRemoteProvider": "cua",
                                                        "desktopRemoteUrl": "wss://desk"})
    with pytest.raises(RunnerCoreError, match="CUA_DESKTOP_REMOTE_TOKEN"):
        manager.desktop_remote()
    with pytest.raises(Exception, match="includes remote"):  # a session cannot start with a remote it cannot reach
        await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})
    manager.defaults = LiveSettings(summary=manager.defaults.summary, belief=manager.defaults.belief, proposer=manager.defaults.proposer,
                                    actor_desktop_source=("remote",), remote_desktop_provider="cua", remote_desktop_url="wss://desk",
                                    remote_desktop_token="t")
    assert manager.desktop_remote() == ("cua", "wss://desk", "t") and manager.desktop_source() == ("remote",)


async def test_the_chat_routes_read_and_reset_the_conversation(tmp_path):
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.get("/api/live/session/chat")).json()["id"] is None  # no session: the same answer, to read
        await live.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})
        empty = await http.get("/api/live/session/chat")
        assert empty.status_code == 200 and empty.json() == {"id": None, "current": None, "agent": None, "turn": 0, "runningRunId": None, "messages": [],
                                                              "pending": [], "conversations": []}
        assert (await http.get(f"/api/live/sessions/{live.session.id}/chat")).json()["conversations"] == []
        assert (await http.post("/api/live/session/chat/messages", json={"text": "hi"})).status_code == 400  # agent missing
        assert (await http.post("/api/live/session/chat/messages", json={"text": "hi", "agent": "cmux", "proposal": "p3"})).status_code == 400  # a proposal reference is {proposalsTs, choice}
        assert (await http.post("/api/live/session/chat/messages", json={"text": "hi", "agent": "cmux", "selectedTasks": [{"taskId": "t1"}]})).status_code == 400  # a picked entry is {taskId, title, text}
        assert (await http.post("/api/live/session/chat/messages", json={"text": "hi", "agent": "cmux"})).json()["code"] == "agent_unavailable"
        assert (await http.post("/api/live/session/chat/reset")).json()["turn"] == 0
        await live.stop("test")


async def test_a_conversation_lives_in_the_store_and_a_later_session_continues_it(tmp_path):
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        await live.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})
        chat, first_session = live.session.chat, live.session.id
        inputs = {"worklog": [], "proposal": None, "preferences": ""}
        first = chat.compose("port the slides", "claude", inputs)
        one = first["chatId"]
        assert first["cwd"] == str(live.chats.workspace(one)) and live.chats.workspace(one).is_dir()  # the agent works in the conversation's folder
        chat.started("r1", first)
        chat.finished("r1", "completed", {"reply": "done", "claudeSessionId": "sess-1", "claudeTotals": {"x": 1}})
        chat.reset()  # a new conversation; the first keeps its state in the store
        second = chat.compose("fix the lint", "codex", inputs)
        two = second["chatId"]
        chat.started("r2", second)
        chat.finished("r2", "completed", {"reply": "ok", "codexThreadId": "thread-2"})
        listed = (await http.get("/api/live/chats")).json()
        assert [item["id"] for item in listed["conversations"]] == [two, one] and listed["current"] == two
        assert [(item["agent"], item["turns"], item["sessions"], item["legacy"], item["viewOnly"]) for item in listed["conversations"]] == [("codex", 1, [first_session], False, False), ("claude", 1, [first_session], False, False)]
        assert (await http.post("/api/live/session/chat/switch", json={"id": one})).json()["current"] == one
        assert (chat.agent, chat.turn, chat.resume, chat.totals) == ("claude", 1, "sess-1", {"sess-1": {"x": 1}})
        assert live.chats.state(two)["resume"] == "thread-2"
        rows = live.chats.transcript(one)
        assert [row["role"] for row in rows] == ["user", "agent"] and rows[0]["sessionId"] == first_session
        assert (await http.post("/api/live/session/chat/switch", json={"id": "nope"})).status_code == 404
        assert (await http.post("/api/live/session/chat/switch", json={})).status_code == 400
        await live.stop("test")
        await live.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})  # a later session
        assert live.session.chat.id == "" and (await http.get("/api/live/chats")).json()["current"] is None  # starts on no conversation
        live.session.chat.switch(one)
        third = live.session.chat.compose("and the footer", "claude", inputs)
        assert third["turn"] == 2 and third["resume"] == "sess-1" and third["chatId"] == one and third["cwd"] == first["cwd"]
        live.session.chat.started("r3", third)
        assert live.chats.summary(one)["sessions"] == [first_session, live.session.id]
        live.session.chat.finished("r3", "completed", {"reply": "ok"})
        picked = [{"taskId": "t1", "title": "ICLR 2027 paper", "text": "Revise the introduction"}]
        fourth = live.session.chat.compose("> ICLR 2027 paper:\n>   • Revise the introduction\n\nDo it today.", "claude", inputs, picked)
        assert fourth["selectedTasks"] == picked and "selectedTasks" not in third
        live.session.chat.started("r4", fourth)
        assert live.chats.transcript(one)[-1]["selectedTasks"] == picked  # the record tells the tray's entries from the person's words
        assert live.memory.stream.events(kinds=("chat_message",))[-1].data["selectedTasks"] == picked
        await live.stop("test")


async def test_conversations_of_sessions_before_the_store_are_listed_and_readable_but_not_continued(tmp_path):
    old = tmp_path / "data" / "sessions" / "20260101-000000"
    old.mkdir(parents=True)
    rows = [
        {"id": "a1", "ts": "2026-01-01T10:00:00Z", "chatId": "old-1", "agent": "claude", "turn": 1, "role": "user", "text": "Port the deck", "context": "", "attached": 0, "runId": "run-1"},
        {"id": "a2", "ts": "2026-01-01T10:01:00Z", "chatId": "old-1", "agent": "claude", "turn": 1, "role": "agent", "text": "Done.", "status": "completed", "runId": "run-1"},
    ]
    (old / "chat.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    (old / "session.json").write_text(json.dumps({"id": "20260101-000000", "name": "Deck week"}))
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        listed = (await http.get("/api/live/chats")).json()
        assert listed["current"] is None
        assert [(c["id"], c["legacy"], c["viewOnly"], c["sessions"], c["turns"], c["firstMessage"], c["title"], c["pinned"]) for c in listed["conversations"]] == [("old-1", True, True, ["20260101-000000"], 1, "Port the deck", None, False)]
        assert listed["sessions"] == {"20260101-000000": "Deck week"}  # the sidebar files it under the session's name
        assert (await http.patch("/api/live/chats/old-1", json={"title": " Deck port "})).json()["conversations"][0]["title"] == "Deck port"  # a legacy one can be named and pinned too
        pinned = (await http.patch("/api/live/chats/old-1", json={"pinned": True})).json()["conversations"][0]
        assert (pinned["title"], pinned["pinned"]) == ("Deck port", True) and live.chats.marks() == {"old-1": {"title": "Deck port", "pinned": True}}
        assert (await http.patch("/api/live/chats/old-1", json={"title": "", "pinned": False})).json()["conversations"][0]["title"] is None and live.chats.marks() == {}
        assert (await http.patch("/api/live/chats/old-1", json={"pinned": "yes"})).status_code == 400
        assert (await http.patch("/api/live/chats/nope", json={"title": "x"})).status_code == 404
        stored = (await http.get("/api/live/sessions/20260101-000000/chat")).json()
        assert stored["id"] == "old-1" and [m["text"] for m in stored["messages"]] == ["Port the deck", "Done."]
        read = (await http.get("/api/live/session/chat?id=old-1")).json()  # the same route with no session: to read
        assert read["current"] is None and [m["text"] for m in read["messages"]] == ["Port the deck", "Done."] and len(read["conversations"]) == 1
        assert (await http.get("/api/live/session/chat")).json()["id"] is None
        assert (await http.post("/api/live/session/chat/messages", json={"text": "more", "agent": "claude"})).status_code == 409
        await live.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})
        assert [m["text"] for m in (await http.get("/api/live/session/chat?id=old-1")).json()["messages"]] == ["Port the deck", "Done."]
        assert (await http.post("/api/live/session/chat/switch", json={"id": "old-1"})).status_code == 409
        await live.stop("test")
