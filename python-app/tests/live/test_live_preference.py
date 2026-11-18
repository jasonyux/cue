"""Preferences: the preference updater's tick and tools, the session wiring, and the preferences block the proposer and a run read."""

from unittest.mock import AsyncMock

import httpx
from live_helpers import FakeClient, fake_settings, make_manager

from app.api import create_app
from app.live.actor.prompts import live_context_text
from app.live.observer.recorder import record as record_row
from app.live.preference.tools import check
from app.live.preference.update import PreferenceUpdater
from app.live.proposer.propose import Proposer
from app.live.storage.memory import MemoryStore
from app.live.storage.paths import session_paths
from app.live.storage.records import TaskOp
from app.live.storage.search import HistoryIndex
from app.live.storage.stream import Cursor
from app.live.utils.jsonl import read_jsonl
from app.runner import RunnerManager

PROPOSED = "2026-10-30T09:59:00Z"
D1, D2 = "0000000000d1", "0000000000d2"  # stream ids are 12 hex characters: the parts are screened by their line heads


def decided(memory, ts, decision, task, *, event_id, reason=None, context=None, session_id="s1"):
    """A decision as the session records it: the event, then its digest and its reason when given."""
    memory.stream.emit("proposal_accepted" if decision == "accept" else "proposal_rejected",
                       {"id": event_id, "ts": ts, "proposalsTs": PROPOSED, "decision": decision, "kind": "act", "task": task},
                       ts=ts, session_id=session_id, id=event_id)
    if context:
        memory.stream.emit("decision_digested", {"proposalsTs": PROPOSED, "decisionTs": ts, "context": context}, ts=ts, session_id=session_id)
    if reason:
        memory.stream.emit("reject_reason_given", {"proposalsTs": PROPOSED, "decisionTs": ts, "reason": reason, "typed": True}, ts=ts, session_id=session_id)


async def test_a_tick_rewrites_the_file_within_the_cap(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = session_paths(tmp_path / "data", "s1").create()
    decided(memory, "2026-10-30T10:00:00Z", "reject", "Draft the reply to Alice", event_id=D1, reason="I write my own emails", context="writing an email in Mail")
    memory.stream.emit("task_flagged", {"id": "f1", "ts": "2026-10-30T10:01:00Z", "taskId": "t001", "title": "Reply to Alice", "step": 1, "text": "send it",
                                        "verdict": "down", "reason": "made up"}, ts="2026-10-30T10:01:00Z", session_id="s1")
    rule = "- No offers to draft emails; they write their own (declined once, Oct 30)."
    client = FakeClient([[("write_preferences", {"text": " ".join(["word"] * 500)})], [("write_preferences", {"text": rule})]])
    updater = PreferenceUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await updater.tick(now="2026-10-30T10:02:00Z")
    system, prompt = (message["content"] for message in client.calls[0]["messages"])
    assert client.calls[0]["tools"] == ["write_preferences"] and client.calls[0]["thinking"] is True  # no history index: no search tool
    assert "<preferences>\n(empty)\n</preferences>" in prompt and "at most 400 tokens" in system and "search_history" not in system
    assert "rejected, I write my own emails (act): Draft the reply to Alice · while: writing an email in Mail" in prompt
    assert 'thumbs down on task "Reply to Alice", entry "send it": made up' in prompt and "<earlier_decisions>" not in prompt
    # The first write is over the cap and rejected with the result the model reads; the second is the file.
    assert update.rounds == 2 and update.changed and update.before == "" and update.after == rule + "\n"
    assert len(update.rejected) == 1 and "the cap is 400" in update.rejected[0]
    assert client.calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_0", "content": update.rejected[0]}
    assert memory.preferences() == rule + "\n" and len(update.feedback) == 4 and D1 in update.feedback and update.observations == 4
    assert read_jsonl(paths.preferences)[0][0]["after"] == rule + "\n" and update.usage["input_tokens"] == 200
    assert memory.stream.events(kinds=("preferences_updated",))[-1].data["changed"] is True
    assert memory.preference_cursor() != Cursor()
    again = PreferenceUpdater(paths, FakeClient(["unused"]), fake_settings(), memory, stream=memory.stream, session_id="s1")
    assert await again.tick(now="2026-10-30T10:03:00Z") is None  # nothing new since the cursor: no call


async def test_a_tick_may_leave_the_file_as_it_is(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = session_paths(tmp_path / "data", "s1").create()
    memory.preferences_file.write_text("- Keeps weekends free.\n")
    decided(memory, "2026-10-30T10:00:00Z", "accept", "Total the budget sheet", event_id=D1)
    first = PreferenceUpdater(paths, FakeClient(["Nothing to change: one acceptance."]), fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await first.tick(now="2026-10-30T10:02:00Z")
    assert update.changed is False and update.after is None and update.before == "- Keeps weekends free.\n" and update.rounds == 1
    assert update.raw == "Nothing to change: one acceptance." and update.rejected == [] and memory.preferences() == "- Keeps weekends free.\n"
    decided(memory, "2026-10-30T10:10:00Z", "accept", "Total the expenses sheet", event_id=D2)
    client = FakeClient(["Still one kind of thing."])
    second = PreferenceUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await second.tick(now="2026-10-30T10:12:00Z")
    prompt = client.calls[0]["messages"][1]["content"]
    assert "<preferences>\n- Keeps weekends free.\n</preferences>" in prompt and update.feedback == [D2]
    reactions, earlier = prompt.split("</reactions>")[0], prompt.split("<earlier_decisions>")[1]
    assert "Total the expenses sheet" in reactions and "Total the budget sheet" not in reactions and "Total the budget sheet" in earlier


async def test_a_tick_may_search_the_history(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = session_paths(tmp_path / "data", "s1").create()
    memory.stream.emit("chat_message", {"runId": "r1", "text": "please never touch my Slack"}, ts="2026-10-29T10:00:00Z", session_id="s0")
    decided(memory, "2026-10-30T10:00:00Z", "reject", "Reply to the Slack thread", event_id=D1)
    index = HistoryIndex(tmp_path / "data" / "memory" / "search.sqlite", memory.stream)
    client = FakeClient([[("search_history", {"query": "slack", "kinds": ["chat"]})], [("write_preferences", {"text": "- Never act in Slack (said in the chat, Oct 29)."})]])
    updater = PreferenceUpdater(paths, client, fake_settings(preference_search_rounds=1), memory, stream=memory.stream, session_id="s1", index=index)
    update = await updater.tick(now="2026-10-30T10:02:00Z")
    assert client.calls[0]["tools"] == ["write_preferences", "search_history"] and "History searchable with search_history" in client.calls[0]["messages"][1]["content"]
    assert "up to 1 time first" in client.calls[0]["messages"][0]["content"]
    assert "never touch my Slack" in client.calls[1]["messages"][-1]["content"]  # the search result, handed back
    assert update.rounds == 2 and len(update.searches) == 1 and update.changed and memory.preferences().startswith("- Never act in Slack")
    index.close()


def test_the_write_tool_check():
    assert check({"name": "write_preferences", "arguments": {"text": "  - A rule.  "}}, cap=400) == ("- A rule.\n", "Accepted.")
    assert check({"name": "write_preferences", "arguments": {"text": "   "}}, cap=400) == (None, "Rejected: text is missing or empty.")
    assert check({"name": "write_preferences", "arguments": {}}, cap=400)[0] is None
    text, result = check({"name": "write_preferences", "arguments": {"text": "one two three four five"}}, cap=2)
    assert text is None and result == "Rejected: the file is 5 tokens; the cap is 2. Cut 3 tokens."


async def test_the_proposer_and_a_run_read_the_preferences(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = session_paths(tmp_path / "data", "s1").create()
    memory.preferences_file.write_text("- No offers to draft emails.\n")
    row = {"kind": "screen_changed", "ts": "2026-10-30T10:00:00Z", "app": "Mail", "window": "Inbox", "tree": "[0] AXWindow"}
    record_row(paths, memory.stream, row)
    client = FakeClient(['{"summary": "s", "proposals": []}'])
    proposer = Proposer(paths, client, fake_settings(), stream=memory.stream, session_id="s1", preferences=memory.preferences_file)
    await proposer.tick(now="2026-10-30T10:01:00Z")
    system, prompt = (message["content"] for message in client.calls[0]["messages"])
    assert "<preferences>\n- No offers to draft emails.\n</preferences>\n\n<tasks>" in prompt and "<preferences>: the person's standing preferences" in system
    record_row(paths, memory.stream, {**row, "ts": "2026-10-30T10:02:00Z", "window": "Sent"})
    off = FakeClient(['{"summary": "s", "proposals": []}'])
    without = Proposer(paths, off, fake_settings(proposer_inputs=("tasks", "worklog")), stream=memory.stream, session_id="s1", preferences=memory.preferences_file)
    await without.tick(now="2026-10-30T10:03:00Z")
    assert "preferences" not in off.calls[0]["messages"][1]["content"]  # left out of proposer.inputs
    # A run's live context carries the file after the worklog and the latest proposal.
    text = live_context_text({"worklog": [], "proposal": None, "preferences": "- No offers to draft emails.\n"})
    assert text.endswith("No activity summaries yet.\nThe user's standing preferences for an assistant, from their past decisions; follow them:\n- No offers to draft emails.")
    assert "preferences" not in live_context_text({"worklog": [], "proposal": None, "preferences": ""})


async def test_the_updater_ticks_on_its_timer_once_enough_reactions_wait(tmp_path):
    ranked = '{"summary": "Budget", "proposals": [{"kind": "act", "action": "propose", "task": "Total the budget sheet", "reason": "r", "confidence": 4}]}'
    write = [("write_preferences", {"text": "- Totals the budget sheet themselves."})]
    manager, client, _ = make_manager(tmp_path / "data", ['{"summary": "Edited the budget sheet."}', "Nothing new.", ranked, "totalling the sheet", write])
    started = await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "preferenceIntervalS": 3600, "preferenceMinFeedback": 2, "notify": False})
    session_id, session = started["session"]["id"], manager.session
    paths = session_paths(tmp_path / "data", session_id)
    record_row(paths, manager.memory.stream, {"kind": "screen_changed", "ts": "2026-10-30T10:00:00Z", "app": "Numbers", "window": "Budget", "tree": "[0] AXWindow"})
    result = await manager.propose_now()
    await manager.record_feedback(result["proposals"]["ts"], "reject")
    await manager.wait_for_digests()
    assert read_jsonl(paths.preferences)[0] == []  # a reaction does not tick the updater: its timer does
    assert await manager._preference_tick(session) is None and read_jsonl(paths.preferences)[0] == []  # one reaction, two needed: it waits
    manager.memory.apply([TaskOp(op="add", title="Budget")], now="2026-10-30T10:05:00Z", session_id=session_id, call_id="c", cursor=None,
                         stale_days=7, done_step_days=21, trash_days=7)
    manager.flag_task("t001", None, "down")
    update = await manager._preference_tick(session)
    assert update is not None and update.changed and manager.memory.preferences() == "- Totals the budget sheet themselves.\n"
    rows = read_jsonl(paths.preferences)[0]
    assert len(rows) == 1 and rows[0]["model"] == "fake-model" and len(rows[0]["feedback"]) == 3  # the decision's two events and the thumbs, kept since the skipped tick
    prompt = client.calls[-1]["messages"][1]["content"]
    assert "totalling the sheet" in prompt and 'thumbs down on task "Budget"' in prompt and client.calls[-1]["tools"] == ["write_preferences", "search_history"]
    assert manager.status()["session"]["usage"]["byModel"][0]["inputTokens"] == 500  # the tick counted with the session's other calls
    assert await manager._preference_tick(session) is None and session.last_error is None  # nothing new
    await manager.stop()


async def test_a_thumbs_over_http_waits_for_the_timer_or_the_stop(tmp_path):
    """The route records the thumbs on the loop; the stop's final tick folds it in."""
    live, _client, _ = make_manager(tmp_path / "data", ["Nothing to change."] * 3)
    runner = RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[])
    app = create_app(manager=runner, live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.post("/api/live/session", json={"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False})).status_code == 202
        session_id = (await http.get("/api/live/session")).json()["session"]["id"]
        paths = session_paths(tmp_path / "data", session_id)
        live.memory.apply([TaskOp(op="add", title="Budget")], now="2026-10-30T10:05:00Z", session_id=session_id, call_id="c", cursor=None,
                          stale_days=7, done_step_days=21, trash_days=7)
        task_id = (await http.get("/api/live/session")).json()["tasks"][0]["id"]
        flagged = await http.post("/api/live/tasks/feedback", json={"taskId": task_id, "step": None, "verdict": "up"})
        assert flagged.status_code == 200 and flagged.json()["verdict"] == "up" and read_jsonl(paths.preferences)[0] == []
        reasoned = await http.post("/api/live/tasks/feedback/reason", json={"flagId": flagged.json()["id"], "reason": "made up"})
        assert reasoned.status_code == 400  # a thumbs up takes no reason
        assert (await http.post("/api/live/session/stop")).status_code == 200
        rows = read_jsonl(paths.preferences)[0]
        assert len(rows) == 1 and rows[0]["changed"] is False and 'thumbs up on task "Budget"' in _client.calls[-1]["messages"][1]["content"]
