"""The event stream: one envelope per event in daily parts, the readers' cursor, windows, and the store's cursor."""

import json

import pytest

from app.live.actor.chat import Chat, ChatStore
from app.live.storage.backfill_ids import main as backfill
from app.live.storage.memory import MemoryStore
from app.live.storage.paths import SessionPaths
from app.live.storage.records import StepSpec, TaskOp
from app.live.storage.stream import Cursor, Stream
from app.live.utils.jsonl import append_jsonl, read_json, read_jsonl


def test_chat_turns_are_events_the_window_folds_into_turns(tmp_path, monkeypatch):
    ticks = iter(f"2026-09-17T10:0{minute}:00Z" for minute in (1, 2, 3))
    monkeypatch.setattr("app.live.actor.chat.timestamp", lambda: next(ticks))
    stream = Stream(tmp_path / "stream")
    chat = Chat(SessionPaths(tmp_path / "sessions" / "s1").create(), ChatStore(MemoryStore(tmp_path)), persist=lambda: None, stream=stream)
    nothing = {"worklog": [], "proposal": None}  # nothing observed to send along
    chat.started("r1", chat.compose("Book the Sydney flight", "claude", nothing))
    chat.finished("r1", "completed", {"reply": "Booked for Dec 3."})
    chat.started("r2", chat.compose("Now the hotel", "claude", nothing))
    assert [event.kind for event in stream.events()] == ["chat_message", "chat_reply", "chat_message"]
    window = stream.window_at("2026-09-17T10:00:00Z", "2026-09-17T10:10:00Z", "s1", lookback_days=1)
    folded = [(item["text"], item["reply"], item["status"]) for item in window.chat()]
    assert folded == [("Book the Sydney flight", "Booked for Dec 3.", "completed"), ("Now the hotel", None, None)]  # a reply joins its message
    assert window.chat(minutes=60) == window.chat() and window.chat(minutes=0) == []


def test_a_chat_hands_the_next_turn_the_totals_of_the_session_it_resumes(tmp_path):
    paths = SessionPaths(tmp_path / "sessions" / "s1").create()
    store = ChatStore(MemoryStore(tmp_path))  # the conversations, kept across sessions
    chat = Chat(paths, store, persist=lambda: None, stream=Stream(tmp_path / "stream"))
    totals = {"claude-opus-5-5": {"inputTokens": 5, "outputTokens": 9, "costUSD": 0.4}}
    chat.started("r1", chat.compose("Fix the chat box", "claude", {"worklog": [], "proposal": None}))
    chat.finished("r1", "completed", {"reply": "Fixed.", "claudeSessionId": "sess-1", "claudeTotals": totals})
    assert chat.compose("Now test it", "claude", {"worklog": [], "proposal": None})["resumeTotals"] == totals
    assert Chat(paths, store, persist=lambda: None, stream=Stream(tmp_path / "stream")).totals == {"sess-1": totals}  # kept in the store
    chat.reset()
    assert "resumeTotals" not in chat.compose("Something else", "claude", {"worklog": [], "proposal": None})


def test_stream_keeps_every_event_in_one_envelope_in_daily_parts(tmp_path):
    stream = Stream(tmp_path / "stream")
    assert stream.events() == [] and stream.read(Cursor()) == ([], Cursor()) and stream.end_cursor() == Cursor()
    with pytest.raises(ValueError, match="Unknown stream event kind"):
        stream.emit("whatever", {}, ts="2026-10-01T10:00:00Z")
    stream.emit("screen_changed", {"kind": "screen_changed", "ts": "2026-10-01T10:00:00Z", "app": "Code"}, ts="2026-10-01T10:00:00Z", session_id="s1")
    summary = stream.emit("summary_written", {"summary": "Edited prompts.py"}, ts="2026-10-01T10:01:00Z", session_id="s1", call_id="c1")
    stream.emit("belief_updated", {"ops": [], "tasks": [{"id": "t001", "title": "Prompts"}]}, ts="2026-10-01T10:02:00Z", session_id="s1", call_id="c2")
    stream.emit("tasks_edited", {"ops": [], "tasks": [{"id": "t001", "title": "Prompts, renamed"}]}, ts="2026-10-01T10:03:00Z")
    stream.emit("screen_changed", {"kind": "screen_changed", "ts": "2026-10-02T09:00:00Z", "app": "Mail"}, ts="2026-10-02T09:00:00Z", session_id="s2")
    assert [part.name for part in stream.parts()] == ["2026-10-01.jsonl", "2026-10-02.jsonl"]  # one part per day of the event's ts
    rows = read_jsonl(stream.parts()[0])[0]
    assert len(summary.id) == 12 and rows[1] == {"id": summary.id, "ts": "2026-10-01T10:01:00Z", "kind": "summary_written", "sessionId": "s1",
                                                 "callId": "c1", "data": {"summary": "Edited prompts.py"}}
    assert [event.kind for event in stream.events()] == ["screen_changed", "summary_written", "belief_updated", "tasks_edited", "screen_changed"]
    assert [event.session_id for event in stream.events(kinds=("screen_changed",))] == ["s1", "s2"]
    assert [event.kind for event in stream.events(session_id="s1")] == ["screen_changed", "summary_written", "belief_updated"]
    assert [event.ts for event in stream.events(since="2026-10-01T10:03:00Z")] == ["2026-10-01T10:03:00Z", "2026-10-02T09:00:00Z"]


def test_stream_cursor_reads_what_arrived_across_parts(tmp_path):
    stream = Stream(tmp_path / "stream")
    stream.emit("summary_written", {"summary": "a"}, ts="2026-10-01T10:00:00Z")
    events, cursor = stream.read(Cursor())
    assert [e.data["summary"] for e in events] == ["a"] and cursor == stream.end_cursor() and cursor.part == "2026-10-01"
    assert stream.read(cursor) == ([], cursor)
    stream.emit("summary_written", {"summary": "b"}, ts="2026-10-01T11:00:00Z")
    stream.emit("summary_written", {"summary": "c"}, ts="2026-10-02T08:00:00Z")
    events, cursor = stream.read(cursor)  # the rest of the old part, then the new part from its start
    assert [e.data["summary"] for e in events] == ["b", "c"] and cursor.part == "2026-10-02" and cursor == stream.end_cursor()
    assert Cursor.parse(cursor.wire()) == cursor and Cursor.parse(None) == Cursor() and Cursor.parse(1234) == Cursor()


def test_window_reads_its_own_days_and_looks_back_a_bounded_time(tmp_path):
    stream = Stream(tmp_path / "stream")
    stream.emit("belief_updated", {"ops": [], "tasks": [{"id": "t001", "title": "Old but standing"}]}, ts="2026-08-01T10:00:00Z")
    stream.emit("proposal_rejected", {"ts": "2026-08-01T11:00:00Z", "proposalsTs": "p0", "decision": "reject", "kind": "act", "task": "Ancient"},
                ts="2026-08-01T11:00:00Z", session_id="s0")
    stream.emit("summary_written", {"ts": "2026-10-01T10:01:00Z", "windowStart": "2026-10-01T10:00:00Z", "windowEnd": "2026-10-01T10:01:00Z",
                                    "summary": "yesterday"}, ts="2026-10-01T10:01:00Z", session_id="s1")
    stream.emit("screen_changed", {"kind": "screen_changed", "ts": "2026-10-02T09:00:00Z", "app": "Mail", "tree": "x"}, ts="2026-10-02T09:00:00Z", session_id="s1")
    stream.emit("run_started", {"ts": "2026-10-02T09:00:30Z", "runId": "r1", "event": "started", "prompt": "Fix it"}, ts="2026-10-02T09:00:30Z", session_id="s1")
    window = stream.window_at("2026-10-02T09:00:00Z", "2026-10-02T09:05:00Z", "s1", lookback_days=30)
    assert [row.app for row in window.observer_rows()] == ["Mail"] and window.running() == ["Fix it"]
    assert [item.summary for item in window.worklog(60 * 24 * 2)] == ["yesterday"]  # within the lookback and the minutes
    assert window.decisions(3, 3) == []  # the rejection is older than the lookback
    assert [task.title for task in window.tasks()] == ["Old but standing"]  # the task list is state: found however far back
    assert [d.task for d in stream.window_at("2026-10-02T09:00:00Z", "2026-10-02T09:05:00Z", "s1", lookback_days=90).decisions(3, 3)] == ["Ancient"]
    earlier = stream.window_at("2026-08-01T10:00:00Z", "2026-08-01T10:00:00Z", "s0", lookback_days=30)  # strictly before the end
    assert earlier.tasks() == []


def test_memory_store_keeps_the_belief_cursor_with_the_task_list(tmp_path):
    memory = MemoryStore(tmp_path).create()
    assert memory.stream.root.is_dir() and memory.stream_cursor() == Cursor()
    rules = {"stale_days": 3, "done_step_days": 7, "trash_days": 7}
    memory.apply(
        [TaskOp(op="add", title="Finish Table 2", steps=[StepSpec(text="numbers")])],
        now="2026-10-01T10:00:00Z",
        session_id="s1",
        call_id="c1",
        cursor=Cursor("2026-10-01", 10),
        **rules,
    )
    stored = read_json(memory.tasks_file)
    assert stored["tasks"][0]["title"] == "Finish Table 2" and stored["streamCursor"] == {"part": "2026-10-01", "offset": 10}
    assert memory.stream_cursor() == Cursor("2026-10-01", 10)
    memory.set_stream_cursor(Cursor("2026-10-02", 99))  # the converter: everything before this counts as read
    assert memory.stream_cursor() == Cursor("2026-10-02", 99) and [task.title for task in memory.tasks()] == ["Finish Table 2"]
    assert memory.stream.events() == []  # the store keeps state; the stages and the session write the events


def test_an_event_reuses_the_id_its_row_was_written_with(tmp_path):
    stream = Stream(tmp_path / "stream")
    given = stream.emit("run_started", {"runId": "r1"}, ts="2026-09-17T10:00:00Z", session_id="s", id="0123456789ab")
    minted = stream.emit("run_finished", {"runId": "r1"}, ts="2026-09-17T10:01:00Z", session_id="s")
    assert given.id == "0123456789ab" and len(minted.id) == 12 and minted.id != given.id
    assert [event.id for event in stream.events()] == [given.id, minted.id]


def test_the_backfill_gives_old_rows_their_events_ids_and_leaves_the_stream_alone(tmp_path, capsys):
    memory = MemoryStore(tmp_path).create()
    old, live = tmp_path / "sessions" / "20260901-100000", tmp_path / "sessions" / "20260902-100000"
    for folder, stopped in ((old, "2026-09-01T11:00:00Z"), (live, None)):
        SessionPaths(folder).create()
        (folder / "session.json").write_text(json.dumps({"id": folder.name, "stoppedAt": stopped}), encoding="utf-8")
    row = {"kind": "screen_changed", "ts": "2026-09-01T10:00:00Z", "app": "Mail", "window": "Inbox", "tree": "x"}
    append_jsonl(SessionPaths(old).snapshots, row)
    seen = memory.stream.emit("screen_changed", row, ts=row["ts"], session_id=old.name)
    line = {"ts": "2026-09-01T10:01:00Z", "windowStart": "2026-09-01T10:00:00Z", "windowEnd": "2026-09-01T10:01:00Z", "summary": "Read the mail."}
    append_jsonl(SessionPaths(old).summaries, line)
    append_jsonl(memory.summaries, {**line, "sessionId": old.name})
    written = memory.stream.emit("summary_written", line, ts=line["ts"], session_id=old.name)
    append_jsonl(SessionPaths(old).chat, {"ts": "2026-09-01T10:02:00Z", "role": "user", "text": "Hi", "runId": "r1"})  # from before the chat was in the stream
    append_jsonl(SessionPaths(live).summaries, {**line, "ts": "2026-09-02T10:01:00Z"})
    memory.stream.emit("summary_written", {**line, "ts": "2026-09-02T10:01:00Z"}, ts="2026-09-02T10:01:00Z", session_id=live.name)
    before = [part.read_bytes() for part in memory.stream.parts()]
    assert backfill(["--data-root", str(tmp_path)]) == 0
    assert "would write 3 ids: 2 from the stream, 1 minted" in capsys.readouterr().out
    assert "id" not in read_jsonl(SessionPaths(old).summaries)[0][0]  # a dry run
    assert backfill(["--data-root", str(tmp_path), "--write"]) == 0
    out = capsys.readouterr().out
    assert f"{live.name}: live, skipped" in out and "memory/summaries.jsonl" not in out  # the memory's files wait too: the backend appends to them
    assert read_jsonl(SessionPaths(old).snapshots)[0][0]["id"] == seen.id and read_jsonl(SessionPaths(old).summaries)[0][0]["id"] == written.id
    assert len(read_jsonl(SessionPaths(old).chat)[0][0]["id"]) == 12 and "id" not in read_jsonl(SessionPaths(live).summaries)[0][0]
    assert [part.read_bytes() for part in memory.stream.parts()] == before  # the stream is not rewritten
    (live / "session.json").write_text(json.dumps({"id": live.name, "stoppedAt": "2026-09-02T11:00:00Z"}), encoding="utf-8")
    assert backfill(["--data-root", str(tmp_path), "--write"]) == 0
    assert read_jsonl(memory.summaries)[0][0]["id"] == written.id and len(read_jsonl(SessionPaths(live).summaries)[0][0]["id"]) == 12
    assert backfill(["--data-root", str(tmp_path), "--write"]) == 0 and "would write" not in capsys.readouterr().out  # nothing left: a second run changes nothing


def test_the_window_hands_every_record_its_events_id_for_citing(tmp_path):
    stream = Stream(tmp_path / "stream")
    line = stream.emit("summary_written", {"ts": "2026-09-17T10:01:00Z", "windowStart": "2026-09-17T10:00:00Z", "windowEnd": "2026-09-17T10:01:00Z", "summary": "Read."},
                       ts="2026-09-17T10:01:00Z", session_id="s")  # a row from before ids: the envelope's serves
    own = stream.emit("summary_written", {"id": "feedfacefeed", "ts": "2026-09-17T10:02:00Z", "windowStart": "2026-09-17T10:01:00Z", "windowEnd": "2026-09-17T10:02:00Z", "summary": "Wrote."},
                      ts="2026-09-17T10:02:00Z", session_id="s", id="feedfacefeed")
    made = stream.emit("proposals_made", {"ts": "2026-09-17T10:03:00Z", "proposals": [{"kind": "act", "task": "Send it"}]}, ts="2026-09-17T10:03:00Z", session_id="s")
    decided = stream.emit("proposal_accepted", {"ts": "2026-09-17T10:04:00Z", "proposalsTs": "2026-09-17T10:03:00Z", "decision": "accept", "kind": "act", "task": "Send it"},
                          ts="2026-09-17T10:04:00Z", session_id="s")
    asked = stream.emit("chat_message", {"ts": "2026-09-17T10:05:00Z", "runId": "c1", "text": "Hi"}, ts="2026-09-17T10:05:00Z", session_id="s")
    window = stream.window_at("2026-09-17T10:00:00Z", "2026-09-17T10:06:00Z", "s", lookback_days=1)
    assert [item.id for item in window.last_worklog(5)] == [line.id, "feedfacefeed"] and own.id == "feedfacefeed"
    assert window.decisions(1, 1)[0].id == decided.id and window.undecided(1) == [] and window.chat()[0]["id"] == asked.id
    stream.emit("proposals_made", {"ts": "2026-09-17T10:05:30Z", "proposals": [{"kind": "act", "task": "File it"}]}, ts="2026-09-17T10:05:30Z", session_id="s")
    pending = stream.window_at("2026-09-17T10:00:00Z", "2026-09-17T10:06:00Z", "s", lookback_days=1).undecided(1)
    assert len(pending) == 1 and len(pending[0].id) == 12 and pending[0].id != made.id  # an unanswered proposal cites its round
