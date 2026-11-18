"""Belief: the memory store's rules, the belief updater's tick, the session wiring, and the proposer's tasks section."""

import re

import pytest
from live_helpers import FakeClient, fake_settings, make_manager

from app.errors import RunnerCoreError
from app.live.belief.prompts import removed_lines
from app.live.belief.tools import ENTRY_LIMIT, MAX_ROUNDS, TITLE_LIMIT, check, encoding, too_long
from app.live.belief.update import BeliefUpdater
from app.live.observer.recorder import record as record_row
from app.live.replay import replay
from app.live.storage.memory import MemoryStore
from app.live.storage.ops import new_task_id as real_new_task_id
from app.live.storage.paths import SessionPaths
from app.live.storage.records import RewordSpec, Step, StepSpec, Summary, Task, TaskFlag, TaskOp, normalize_op
from app.live.storage.stream import Cursor, Stream
from app.live.utils.jsonl import append_jsonl, read_json, read_jsonl, tail_jsonl
from app.live.utils.prompting import task_lines


def line(index, text):
    return Summary(ts=f"2026-09-23T01:1{index}:00Z", window_start=f"2026-09-23T01:0{index}:00Z",
                   window_end=f"2026-09-23T01:1{index}:00Z", summary=text)


def test_memory_store_applies_ops_and_enforces_the_rules(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    summary = line(0, "Edited Table 2.")
    assert memory.append_summary(summary, "s1") is summary and read_jsonl(memory.summaries)[0][0]["sessionId"] == "s1"
    assert memory.stream_cursor() == Cursor()
    cursor = Cursor("2026-09-23", 120)  # where in the stream the belief updater has read up to: kept next to the list
    now = "2026-09-23T01:20:00Z"
    rules = {"stale_days": 3, "done_step_days": 7, "trash_days": 7, "call_id": "call-1"}
    tasks = memory.apply([TaskOp(op="add", title="Finish Table 2", evidence=["01:14"],
                                 steps=[StepSpec(text="collect numbers"), StepSpec(text="draft layout"), StepSpec(text="outline", done=True)]),
                          TaskOp(op="add", title="Reply to Alice"),
                          TaskOp(op="update", task_id="t999", steps=[StepSpec(text="unknown id: ignored")])],
                         now=now, session_id="s1", cursor=cursor, **rules)
    assert [(t.id, t.title, t.status) for t in tasks] == [("t001", "Finish Table 2", "active"), ("t002", "Reply to Alice", "active")]
    assert [(s.text, s.done_at) for s in tasks[0].steps] == [("collect numbers", None), ("draft layout", None), ("outline", now)]
    assert tasks[0].evidence == ["01:14"] and tasks[0].session_id == "s1"
    assert tasks[0].call_id == "call-1" and tasks[0].steps[2].call_id == "call-1"  # the call that added them
    assert memory.stream_cursor() == cursor and read_json(memory.tasks_file)["streamCursor"] == cursor.wire()
    events = read_jsonl(memory.events)[0]
    assert [e["op"] for e in events] == ["add", "add"] and events[0]["sessionId"] == "s1"
    later = "2026-09-27T09:00:00Z"
    tasks = memory.apply([TaskOp(op="status", task_id="t001", step=1, done=True), TaskOp(op="status", task_id="t001", step=9, done=True),  # 9: no such entry
                          TaskOp(op="update", task_id="t001", steps=[StepSpec(text="Draft Layout")]),  # a duplicate: skipped
                          TaskOp(op="update", task_id="t002", steps=[StepSpec(text="drafted the reply", done=True), StepSpec(text="send it")])],
                         now=later, session_id="s2", cursor=cursor, **{**rules, "call_id": "call-2"})
    # Pending first, then done newest first.
    assert [(s.text, s.done_at) for s in tasks[0].steps] == [("draft layout", None), ("collect numbers", later), ("outline", now)]
    assert [(s.text, s.done_at) for s in tasks[1].steps] == [("send it", None), ("drafted the reply", later)]
    assert tasks[1].steps[0].call_id == "call-2" and tasks[1].call_id == "call-1"
    assert [(e["op"], e["callId"]) for e in read_jsonl(memory.events)[0]][-3:] == [("status", "call-2"), ("update", "call-2"), ("update", "call-2")]
    assert tasks[1].session_id == "s2" and tasks[1].last_seen == later
    aged = memory.apply([TaskOp(op="status", task_id="t001", done=True)], now="2026-10-05T09:00:00Z", session_id="s3", cursor=cursor, **rules)
    assert [t.status for t in aged] == ["done", "paused"]  # t002 untouched for days: paused by the store, never deleted
    assert [s.text for s in aged[0].steps] == ["draft layout", "collect numbers", "outline"]  # finished entries are kept for good
    assert [s.text for s in aged[1].steps] == ["send it", "drafted the reply"]
    revived = memory.apply([TaskOp(op="status", task_id="t002", step=1, done=True), TaskOp(op="add", title="New")],
                           now="2026-10-05T09:01:00Z", session_id="s3", cursor=cursor, **rules)
    assert [(t.id, t.status) for t in revived] == [("t001", "done"), ("t002", "active"), ("t003", "active")]
    assert revived[1].steps[0].done_at == "2026-10-05T09:01:00Z"
    undone = memory.apply([TaskOp(op="status", task_id="t002", step=1, done=False), TaskOp(op="status", task_id="t001", done=False)],
                          now="2026-10-05T09:02:00Z", session_id="s3", cursor=cursor, **rules)
    assert undone[1].steps[0].done_at is None and undone[0].status == "active"  # unticked; the task reopened
    trimmed = memory.apply([TaskOp(op="update", task_id="t002", remove=[1, 9], steps=[StepSpec(text="ship it")])],
                           now="2026-10-05T09:03:00Z", session_id="s3", cursor=cursor, **rules)
    assert [(s.text, s.done_at) for s in trimmed[1].steps] == [("ship it", None), ("drafted the reply", "2026-09-27T09:00:00Z")]  # the old finished entry stays
    renamed = memory.apply([TaskOp(op="rename", task_id="t002", title="Reply to Alice about the deck")],
                           now="2026-10-05T09:04:00Z", session_id="s3", cursor=cursor, **rules)
    assert renamed[1].title == "Reply to Alice about the deck" and [(s.text, s.done_at) for s in renamed[1].steps] == [("ship it", None), ("drafted the reply", "2026-09-27T09:00:00Z")]
    memory.apply([TaskOp(op="update", task_id="t003", steps=[StepSpec(text="a"), StepSpec(text="b"), StepSpec(text="c")])],
                 now="2026-10-05T09:05:00Z", session_id="s3", cursor=cursor, **rules)
    # Several calls in one reply: numbers mean the list as shown, so removing 1 does not shift what 2 refers to.
    batch = memory.apply([TaskOp(op="update", task_id="t003", remove=[1]), TaskOp(op="status", task_id="t003", step=2, done=True),
                          TaskOp(op="status", task_id="t003", step=1, done=True)],  # entry 1 was removed in this batch: no-op
                         now="2026-10-05T09:06:00Z", session_id="s3", cursor=cursor, **rules)
    assert [(s.text, s.done_at is not None) for s in batch[2].steps] == [("c", False), ("b", True)]


def test_memory_store_soft_deletes_tasks_and_entries(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    rules = {"stale_days": 30, "done_step_days": 30, "trash_days": 7, "call_id": "c"}
    now = "2026-10-01T09:00:00Z"
    memory.apply([TaskOp(op="add", title="Real", steps=[StepSpec(text="a"), StepSpec(text="b")]), TaskOp(op="add", title="Wrong")],
                 now=now, session_id="s", cursor=Cursor(), **rules)
    tasks = memory.apply([TaskOp(op="delete", task_id="t002"), TaskOp(op="update", task_id="t001", remove=[1]),
                          TaskOp(op="delete", task_id="t999")],  # unknown: ignored
                         now=now, session_id="operator", cursor=None, **rules)
    assert [(t.id, [s.text for s in t.steps]) for t in tasks] == [("t001", ["b"])]  # the visible list
    assert [(t.id, t.deleted_at, [s.text for s in t.steps]) for t in memory.trash()] == [("t001", None, ["a"]), ("t002", now, [])]
    assert [t.id for t in memory.tasks()] == ["t001"] and read_json(memory.tasks_file)["tasks"][1]["deletedAt"] == now
    memory.apply([TaskOp(op="status", task_id="t001", step=1, done=True), TaskOp(op="add", title="Room for one more")],
                 now=now, session_id="s", cursor=Cursor(), **rules)  # numbers mean the visible list: entry 1 is "b"; the trash is not counted
    assert [(s.text, s.done_at) for s in memory.tasks()[0].steps] == [("b", now)] and len(memory.tasks()) == 2
    restored = memory.apply([TaskOp(op="restore", task_id="t002"), TaskOp(op="restore", task_id="t001", step=1),
                             TaskOp(op="restore", task_id="t001", step=9)],  # no such trashed entry: ignored
                            now=now, session_id="operator", cursor=None, **rules)
    assert [(t.id, [s.text for s in t.steps]) for t in restored] == [("t001", ["a", "b"]), ("t002", []), ("t003", [])]
    assert memory.trash() == []
    memory.apply([TaskOp(op="delete", task_id="t002")], now=now, session_id="s", cursor=Cursor(), **rules)
    assert [t.id for t in memory.trash()] == ["t002"]
    memory.apply([], now="2026-10-09T09:00:00Z", session_id="s", cursor=Cursor(), **rules)  # the trash empties after trash_days
    assert memory.trash() == [] and [t.id for t in memory.tasks()] == ["t001", "t003"]
    assert [e["op"] for e in read_jsonl(memory.events)[0]][2:] == ["delete", "update", "status", "add", "restore", "restore", "delete"]


def test_real_task_ids_are_random_and_short():
    ids = {real_new_task_id() for _ in range(1000)}  # the real generator (the fixture numbers ids only for the other tests)
    assert len(ids) == 1000 and all(re.fullmatch(r"t[0-9a-f]{8}", task_id) for task_id in ids)


async def test_belief_updater_reads_memory_and_revises_the_task_list(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = SessionPaths(tmp_path / "data" / "sessions" / "s1").create()
    for index, text in enumerate(("Edited Table 2 of the paper.", "Replied to Alice in Slack.")):
        memory.stream.emit("summary_written", line(index, text).wire(), ts=line(index, text).ts, session_id="s1")
    memory.stream.emit("chat_message", {"ts": "2026-09-23T01:15:00Z", "runId": "c1", "chatId": "c1", "turn": 1, "agent": "claude", "role": "user", "text": "Fix the table's caption"},
                       ts="2026-09-23T01:15:00Z", session_id="s1")
    memory.stream.emit("chat_reply", {"ts": "2026-09-23T01:16:00Z", "runId": "c1", "chatId": "c1", "turn": 1, "agent": "claude", "role": "agent", "text": "Done.", "status": "completed"},
                       ts="2026-09-23T01:16:00Z", session_id="s1")
    calls = [("add_task", {"title": "Finish Table 2 of the paper", "evidence": ["01:10"],
                           "steps": [{"text": "collect numbers"}, {"text": "outlined the table", "done": True}]}),
             ("toggle_task_status", {"task_id": "nope", "done": True}), ("add_task", {"steps": ["no title: dropped"]}), ("bogus", {"x": 1})]
    client = FakeClient([calls, "Nothing to fix.", "Nothing new."])
    memory.preferences_file.write_text("- Keeps the paper's work under one task.\n")  # the preference updater's file: shown first
    updater = BeliefUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await updater.tick(now="2026-09-23T01:20:00Z")
    assert client.calls[0]["messages"][1]["content"].split("\n\n")[1] == "<preferences>\n- Keeps the paper's work under one task.\n</preferences>"
    assert "- <preferences>:" in client.calls[0]["messages"][0]["content"]
    # The good call is applied at once; the three bad ones are rejected with a reason each, and the model gets a second
    # reply to fix them (it chose not to). The record holds both rounds: their ops, their rejections, their usage.
    assert [op.op for op in update.ops] == ["add"] and [t.title for t in update.tasks] == ["Finish Table 2 of the paper"]
    assert update.rounds == 2 and update.rejected == ["Rejected: there is no task nope on the list.",
                                                       "Rejected: a required argument is missing or malformed.",
                                                       "Rejected: there is no tool named bogus."]
    assert update.raw == "Nothing to fix." and update.usage["input_tokens"] == 200 and update.usage["output_tokens"] == 40
    assert update.observations == 2 and update.model == "fake-model" and client.calls[0]["thinking"] is True
    assert "<chat>\n- [" in client.calls[0]["messages"][1]["content"] and "asked the assistant: Fix the table's caption\n  it answered (completed): Done." in client.calls[0]["messages"][1]["content"]
    assert len(update.call_id) == 12 and update.tasks[0].call_id != update.call_id  # the task was added by the first round's call
    assert update.tasks[0].call_id == tail_jsonl(paths.model_calls, 5)[-2]["id"] and update.call_id == tail_jsonl(paths.model_calls, 5)[-1]["id"]
    assert client.calls[0]["tools"] == ["add_task", "update_task_checklist", "toggle_task_status", "rename_task", "delete_task"]
    prompt = client.calls[0]["messages"][1]["content"]
    assert "Edited Table 2" in prompt and "Replied to Alice" in prompt and "(none yet)" in prompt
    # The second round continues the conversation: the model's own reply with its calls, then one result per call.
    second = client.calls[1]["messages"]
    assert second[:2] == client.calls[0]["messages"] and second[2]["role"] == "assistant" and second[2]["content"] is None
    assert [(call["id"], call["function"]["name"]) for call in second[2]["tool_calls"]] == [("call_0", "add_task"), ("call_1", "toggle_task_status"),
                                                                                           ("call_2", "add_task"), ("call_3", "bogus")]
    assert [(m["role"], m["tool_call_id"], m["content"]) for m in second[3:]] == [("tool", "call_0", "Accepted."), ("tool", "call_1", update.rejected[0]),
                                                                                  ("tool", "call_2", update.rejected[1]), ("tool", "call_3", update.rejected[2])]
    assert [t.title for t in memory.tasks()] == ["Finish Table 2 of the paper"]
    stored = tail_jsonl(paths.beliefs, 5)
    assert len(stored) == 1 and stored[0]["tasks"][0]["id"] == "t001" and stored[0]["ops"][0]["op"] == "add" and stored[0]["rounds"] == 2
    assert [row["kind"] for row in tail_jsonl(paths.model_calls, 5)] == ["belief", "belief"]  # one logged call per round
    assert memory.stream.events()[-1].kind == "belief_updated" and memory.stream.events()[-1].call_id == update.call_id
    assert memory.stream.window_at("2026-09-23T01:20:00Z", "2026-09-23T01:21:00Z", "s1", lookback_days=30).tasks()[0].id == "t001"
    assert await updater.tick(now="2026-09-23T01:25:00Z") is None  # nothing new in the stream: no call
    memory.stream.emit("summary_written", line(2, "Sent the deck.").wire(), ts=line(2, "x").ts, session_id="s1")
    quiet = await updater.tick(now="2026-09-23T01:30:00Z")  # a reply without tool calls: nothing changed, one round
    assert quiet.ops == [] and quiet.raw == "Nothing new." and quiet.tasks is None and quiet.rounds == 1 and quiet.rejected == []  # no ops: the row goes without the list
    assert memory.stream_cursor().part == "2026-09-23" and 0 < memory.stream_cursor().offset < memory.stream.end_cursor().offset  # read up to its own record
    listed = client.calls[2]["messages"][1]["content"]
    assert "[t001] active · Finish Table 2 of the paper" in listed and "1. [ ] collect numbers" in listed
    assert "2. [x] outlined the table (done 2026-09-23)" in listed


LONG_ENTRY = ("Get the Cua sandbox working in sb_test.ipynb through the persistent WebSocket RPC service in the cua-actor container "
              "behind the Cloudflare tunnel")  # 21 words / 30 tokens: over both measures


def test_the_length_check_passes_text_that_fits_either_measure():
    dense_title = "Fix the cua-actor.xyutests.us WebSocket 403 in sb_test.ipynb"  # 7 words, 19 tokens
    assert len(encoding().encode(dense_title)) > TITLE_LIMIT[1] and too_long("The title", dense_title, TITLE_LIMIT) is None
    assert too_long("The title", "Get the Google One student offer for the year", TITLE_LIMIT) is None  # 9 words, 9 tokens
    long_title = "Work on the NeurIPS 2026 paper (Reinforcement World Model Learning for LLM-based Agents)"
    assert too_long("The title", long_title, TITLE_LIMIT) == (f'The title "{long_title}" is 13 words / 23 tokens; the limit is 7 words or 15 tokens. '
                                                               "Shorten it by 6 words (8 tokens).")
    dense_entry = "fix the cua-actor.xyutests.us and vllm12610.xyutests.us ingress 403"  # 7 words, token-heavy
    assert len(encoding().encode(dense_entry)) > ENTRY_LIMIT[1] and too_long("The entry", dense_entry, ENTRY_LIMIT) is None
    assert too_long("The entry", LONG_ENTRY, ENTRY_LIMIT) == (f'The entry "{LONG_ENTRY}" is 21 words / 30 tokens; the limit is 10 words or 20 tokens. '
                                                               "Shorten it by 11 words (10 tokens).")
    # A call is applied whole or not at all: one bad entry rejects the add, naming every problem.
    op, result = check({"id": "c", "name": "add_task", "arguments": {"title": "Ship the sandbox", "steps": [{"text": "write the client"}, {"text": LONG_ENTRY}]}}, [])
    assert op is None and result.startswith("Rejected. The entry ") and result.count("Shorten it") == 1
    op, result = check({"id": "c", "name": "rename_task", "arguments": {"task_id": "t001", "title": "Ship the sandbox"}}, [Task(id="t001", title="old")])
    assert op is not None and op.op == "rename" and result == "Accepted."


def test_everything_on_the_list_is_unique():
    """A title already on the list, or an entry already on its task, open, done or in the trash, is rejected with where it is."""
    now = "2026-10-14T21:57:58Z"
    tasks = [Task(id="t001", title="Work on the project", steps=[Step(text="Open one"), Step(text="Done one", done_at=now)])]
    trash = [Task(id="t001", title="Work on the project", steps=[Step(text="Fix the saved config", deleted_at=now)]),
             Task(id="t002", title="Old task", deleted_at=now)]
    def verdict(name, arguments):
        return check({"name": name, "arguments": arguments}, tasks, trash)[1]
    assert verdict("update_task_checklist", {"task_id": "t001", "add": [{"text": "open one"}]}) == 'Rejected. "open one" is already on t001.'
    assert verdict("update_task_checklist", {"task_id": "t001", "add": [{"text": "Done one"}]}) == 'Rejected. "Done one" is already on t001 (done).'
    assert verdict("update_task_checklist", {"task_id": "t001", "add": [{"text": "Fix the saved config"}]}) == (
        'Rejected. "Fix the saved config" was removed from t001 (in its trash).'
    )
    assert verdict("add_task", {"title": "work on the project"}) == 'Rejected. There is already a task "Work on the project" (t001).'
    assert verdict("add_task", {"title": "Old task"}) == 'Rejected. There is already a task "Old task" (t002, in the trash).'
    assert verdict("rename_task", {"task_id": "t001", "title": "Old task"}) == 'Rejected. There is already a task "Old task" (t002, in the trash).'
    assert verdict("rename_task", {"task_id": "t001", "title": "Work on the Project"}) == "Accepted."  # a task may recase its own title
    assert verdict("update_task_checklist", {"task_id": "t001", "add": [{"text": "Something new"}]}) == "Accepted."


def test_the_removed_list_shows_the_newest_few():
    trash = [Task(id="t001", title="Work", steps=[Step(text=f"entry {n}", deleted_at=f"2026-10-{n:02d}T10:00:00Z") for n in range(1, 13)]),
             Task(id="t002", title="Old task", deleted_at="2026-10-14T09:00:00Z")]
    lines = removed_lines(trash, [{"taskId": "t001", "text": "entry 12", "verdict": "down", "reason": "made up"}]).splitlines()
    assert lines[:2] == ['- [t002] task "Old task"', '- [t001] entry "entry 12" (marked made up)'] and len(lines) == 10  # newest first, capped
    assert lines[-1] == '- [t001] entry "entry 4"'


async def test_a_removed_entry_is_shown_as_removed_and_not_added_back(tmp_path):
    """The sequence of 2026-10-14: the model adds an entry, the person marks it made up and removes it, the worklog still
    shows the work. The next update sees the removal with the reason, and a call adding it again is rejected."""
    memory = MemoryStore(tmp_path / "data").create()
    paths = SessionPaths(tmp_path / "data" / "sessions" / "s1").create()
    rules = {"stale_days": 30, "done_step_days": 30, "trash_days": 7, "cursor": None}
    text = "Fix saved config adding an unwanted remote_desktop block"
    memory.apply([TaskOp(op="add", title="Work on the project", steps=[StepSpec(text=text)])], now="2026-10-14T21:55:31Z", session_id="s1", call_id="m1", **rules)
    memory.append_task_flag(TaskFlag(id="f1", ts="2026-10-14T21:57:47Z", task_id="t001", step=1, text=text, verdict="down", reason="made up"))
    edited = memory.apply([TaskOp(op="update", task_id="t001", remove=[1])], now="2026-10-14T21:57:58Z", session_id="operator", call_id="o1", **rules)
    memory.stream.emit("tasks_edited", {"ops": [], "tasks": [task.wire() for task in edited]}, ts="2026-10-14T21:57:58Z", session_id="s1")  # as the page's edit does
    memory.stream.emit("summary_written", line(0, "Fixed the saved config that added a remote_desktop block.").wire(), ts=line(0, "").ts, session_id="s1")
    client = FakeClient([[("update_task_checklist", {"task_id": "t001", "add": [{"text": text}], "evidence": ["01:10"]})], "Nothing new."])
    updater = BeliefUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await updater.tick(now="2026-10-14T21:58:38Z")
    prompt = client.calls[0]["messages"][1]["content"]
    assert f'<removed>\n- [t001] entry "{text}" (marked made up)\n</removed>\n\n<worklog>' in prompt
    assert update.ops == [] and update.rejected == [f'Rejected. "{text}" was removed from t001 (in its trash).']
    assert [step.text for step in memory.tasks()[0].steps] == [] and [s.text for s in memory.trash()[0].steps] == [text]


def test_a_filler_call_is_rejected():
    """Sonnet starts a call it then does not mean and fills the required field with "placeholder" (a call cannot be retracted):
    dropped, and the model is told why, the same way as an overlong title."""
    assert check({"name": "add_task", "arguments": {"title": " Placeholder "}}, []) == (
        None, 'Rejected. The title is the word "placeholder": a call you do not mean is better left out.'
    )
    assert check({"name": "add_task", "arguments": {"title": "Finish Table 2", "steps": [{"text": "placeholder"}]}}, []) == (
        None, 'Rejected. The entry is the word "placeholder": a call you do not mean is better left out.'
    )
    assert check({"name": "add_task", "arguments": {"title": "Replace the placeholder image"}}, [])[1] == "Accepted."  # the word inside a real title is fine


async def test_a_rejected_call_is_fixed_in_the_next_round_and_rounds_are_capped(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = SessionPaths(tmp_path / "data" / "sessions" / "s1").create()
    memory.stream.emit("summary_written", line(0, "Worked on the sandbox notebook.").wire(), ts=line(0, "x").ts, session_id="s1")
    first = [("add_task", {"title": "Get the sandbox working", "steps": [{"text": LONG_ENTRY}]})]
    fixed = [("add_task", {"title": "Get the sandbox working", "steps": [{"text": "run the notebook through the RPC service"}]})]
    client = FakeClient([first, fixed])
    updater = BeliefUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await updater.tick(now="2026-09-23T01:20:00Z")
    assert update.rounds == 2 and len(update.rejected) == 1 and "Shorten it by 11 words" in update.rejected[0]
    assert [(t.title, [s.text for s in t.steps]) for t in update.tasks] == [("Get the sandbox working", ["run the notebook through the RPC service"])]
    assert client.calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_0", "content": update.rejected[0]}
    # A model that never fixes its call gets MAX_ROUNDS replies, then the tick ends with what was accepted: nothing.
    memory.stream.emit("summary_written", line(1, "Still on the notebook.").wire(), ts=line(1, "x").ts, session_id="s1")
    stubborn = FakeClient([first] * MAX_ROUNDS)
    updater = BeliefUpdater(paths, stubborn, fake_settings(), memory, stream=memory.stream, session_id="s1")
    update = await updater.tick(now="2026-09-23T01:30:00Z")
    assert update.rounds == MAX_ROUNDS == len(stubborn.calls) == len(update.rejected) and update.ops == []
    assert len(stubborn.calls[-1]["messages"]) == 2 + 2 * (MAX_ROUNDS - 1)  # the prompt, then a reply and a result per earlier round
    assert [t.title for t in update.tasks] == ["Get the sandbox working"] and update.usage["input_tokens"] == 100 * MAX_ROUNDS


async def test_session_feeds_memory_and_the_proposer_sees_tasks(tmp_path):
    manager, client, _ = make_manager(tmp_path / "data", [
        '{"summary": "Edited Table 2 of the paper."}',
        [("add_task", {"title": "Finish Table 2 of the paper", "steps": ["numbers missing"]})],
        '{"summary": "s", "proposals": []}'])
    await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "beliefIntervalS": 3600, "notify": False})
    session = manager.session
    record_row(session.paths, manager.memory.stream, {"kind": "screen_changed", "ts": "2026-09-23T01:14:00Z", "app": "Overleaf", "window": "paper",
                                                 "tree": "[0] AXWindow"})
    assert await manager._summary_tick(session) is not None
    assert read_jsonl(manager.memory.summaries)[0][-1]["sessionId"] == session.id
    assert "sessionId" not in tail_jsonl(session.paths.summaries, 1)[0]  # the session's own copy is unchanged
    update = await manager._belief_tick(session)
    assert [t.title for t in update.tasks] == ["Finish Table 2 of the paper"] and manager.memory.tasks()[0].id == "t001"
    await manager._proposer_tick(session, force=True)
    system, prompt = (message["content"] for message in client.calls[-1]["messages"])
    assert "<tasks>" in prompt and "[t001] active · Finish Table 2 of the paper" in prompt
    assert "1. [ ] numbers missing" in prompt
    # Operator corrections: the same ops, the same rules, logged as "operator"; the belief cursor is untouched.
    edited = manager.edit_tasks([{"op": "status", "taskId": "t001", "step": 1, "done": True}, {"op": "status", "taskId": "t001", "done": True},
                                 {"op": "bogus"}])
    assert edited["tasks"][0]["status"] == "done" and edited["tasks"][0]["steps"][0]["doneAt"]
    reopened = manager.edit_tasks([{"op": "status", "taskId": "t001", "step": 1, "done": False},
                                   {"op": "status", "taskId": "t001", "done": False}])["tasks"][0]  # a mistake, undone
    assert reopened["status"] == "active" and reopened["steps"][0]["doneAt"] is None
    assert manager.edit_tasks([{"op": "rename", "taskId": "t001", "title": "Table 2"}])["tasks"][0]["title"] == "Table 2"
    edits = manager.memory.stream.events(kinds=("tasks_edited",))  # operator edits carry the list after them, like belief updates
    assert len(edits) == 3 and edits[-1].data["tasks"][0]["title"] == "Table 2" and edits[-1].session_id == session.id
    assert manager.memory.stream.window_at("2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z", session.id, lookback_days=30).tasks()[0].title == "Table 2"
    assert read_jsonl(manager.memory.events)[0][-1]["sessionId"] == "operator" and manager.memory.stream_cursor().offset > 0  # the belief cursor stands
    with pytest.raises(RunnerCoreError):
        manager.edit_tasks([{"op": "bogus"}])
    assert "task list across sessions" in system
    await manager.stop()


async def test_replay_belief_stage_rebuilds_tasks_from_a_stored_session(tmp_path):
    session = SessionPaths(tmp_path / "sessions" / "s1").create()
    stream = Stream(tmp_path / "stream")
    stream.emit("summary_written", line(5, "Edited Table 2.").wire(), ts=line(5, "x").ts, session_id="s1")
    stream.emit("belief_updated", {"ts": "2026-09-23T01:16:00Z", "ops": [], "tasks": [{"id": "t001", "title": "Old task"}]},
                ts="2026-09-23T01:16:00Z", session_id="s1")
    append_jsonl(session.model_calls, {"id": "c1", "ts": "2026-09-23T01:20:00Z", "kind": "belief", "windowStart": "2026-09-23T01:05:00Z",
                                       "windowEnd": "2026-09-23T01:20:00Z"})
    client = FakeClient([[("add_task", {"title": "Finish Table 2", "steps": ["started"]})]])
    replayed = await replay(session, "belief", stream=stream, settings=fake_settings(), client=client, out=tmp_path / "out")
    assert [t.title for t in replayed[0].tasks] == ["Finish Table 2"] and (tmp_path / "out" / "memory" / "tasks.json").is_file()
    prompt = client.calls[0]["messages"][1]["content"]  # the worklog line in the window, and the list as the stream last carried it
    assert "Edited Table 2" in prompt and "Old task" in prompt and len(tail_jsonl(tmp_path / "out" / "beliefs.jsonl", 5)) == 1


def test_the_task_list_shown_to_models_keeps_pending_entries_and_only_recent_done_ones(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    rules = {"stale_days": 30, "done_step_days": 90, "trash_days": 7, "session_id": "s1", "cursor": None}
    memory.apply([TaskOp(op="add", title="Ship the app", steps=[StepSpec(text="write the docs"), StepSpec(text="old work", done=True)])],
                 now="2026-09-01T09:00:00Z", call_id="c1", **rules)
    for day in range(1, 15):  # one entry finished per day, 09-02 to 09-15
        memory.apply([TaskOp(op="update", task_id="t001", steps=[StepSpec(text=f"finished on day {day}", done=True)])],
                     now=f"2026-09-{day + 1:02d}T09:00:00Z", call_id="c2", **rules)
    memory.apply([TaskOp(op="add", title="Empty task")], now="2026-09-15T10:00:00Z", call_id="c3", **rules)
    shown = task_lines(memory.tasks(), now="2026-09-15T12:00:00Z")
    lines = shown.splitlines()
    # The pending entry, then the done ones of the last 3 days newest first, numbered as the store numbers them.
    assert lines[1:5] == ["    1. [ ] write the docs (added 2026-09-01)", "    2. [x] finished on day 14 (done 2026-09-15)",
                         "    3. [x] finished on day 13 (done 2026-09-14)", "    4. [x] finished on day 12 (done 2026-09-13)"]
    assert lines[5] == "    … and 12 older done entries, not shown" and lines[6].startswith("- [t002] active · Empty task")
    assert len(lines) == 7
    # The cap applies when more than 10 were finished within the window.
    recent = memory.tasks()
    for step in recent[0].steps:
        if step.done_at:
            step.done_at = "2026-09-15T08:00:00Z"
    lines = task_lines(recent, now="2026-09-15T12:00:00Z").splitlines()
    assert lines[1] == "    1. [ ] write the docs (added 2026-09-01)" and len([line for line in lines if "[x]" in line]) == 10
    assert lines[12] == "    … and 5 older done entries, not shown"


def test_task_flags_are_recorded_per_task_and_entry(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data")
    manager.edit_tasks([{"op": "add", "title": "Real goal", "steps": [{"text": "first step"}, {"text": "second step"}]}])
    up = manager.flag_task("t001", None, "up")
    assert (up["taskId"], up["step"], up["verdict"], up["title"], up["sessionId"], up["reason"]) == ("t001", None, "up", "Real goal", "", "")
    down = manager.flag_task("t001", 2, "down")
    assert down["text"] == "second step" and down["reason"] == "" and len(down["id"]) == 12
    assert len(down["callId"]) == 12 and down["callId"] == up["callId"]  # the operator edit that added them, in events.jsonl
    with pytest.raises(RunnerCoreError, match="not a thumbs down"):
        manager.flag_task_reason(up["id"], "duplicate")
    assert manager.flag_task_reason(down["id"], "wrong level")["reason"] == "wrong level"
    for bad in (("t999", None, "up"), ("t001", 9, "up"), ("t001", None, "meh")):
        with pytest.raises(RunnerCoreError):
            manager.flag_task(*bad)
    switched = manager.flag_task("t001", None, "down")  # a second click on the same task updates its record: same id, reason dropped
    assert switched["id"] == up["id"] and switched["verdict"] == "down"
    flags = manager.status()["taskFlags"]
    assert [(flag["id"], flag["step"], flag["verdict"], flag["reason"]) for flag in flags] == [
        (up["id"], None, "down", ""), (down["id"], 2, "down", "wrong level")]
    assert len(read_jsonl(manager.memory.task_feedback)[0]) == 2 and manager.status()["tasks"][0]["title"] == "Real goal"  # nothing changed
    assert [event.kind for event in manager.memory.stream.events(kinds=("task_flagged", "task_unflagged"))] == ["task_flagged"] * 4
    manager.edit_tasks([{"op": "update", "taskId": "t001", "remove": [2]}])  # the flagged entry goes to the trash: the flag stays
    assert [(flag["step"], flag["text"]) for flag in read_jsonl(manager.memory.task_feedback)[0]][1] == (2, "second step")
    assert manager.flag_task("t001", 1, "up")["text"] == "first step"  # entry 1 is still entry 1: its own flag, not the old entry 2's
    assert manager.unflag_task(down["id"]) == {"id": down["id"], "deleted": True}  # taken back
    assert [flag["text"] for flag in manager.status()["taskFlags"]] == ["", "first step"]
    with pytest.raises(RunnerCoreError, match="Unknown flag"):
        manager.unflag_task(down["id"])


def test_an_entry_flag_follows_its_text_when_the_list_is_reordered(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data")
    manager.edit_tasks([{"op": "add", "title": "Real goal", "steps": [{"text": "first step"}, {"text": "second step"}]}])
    down = manager.flag_task("t001", 2, "down")
    manager.edit_tasks([{"op": "status", "taskId": "t001", "step": 1, "done": True}])  # finished entries go after the open ones
    assert [step["text"] for step in manager.status()["tasks"][0]["steps"]] == ["second step", "first step"]
    again = manager.flag_task("t001", 1, "up")  # the same entry, now entry 1: its record is updated, no second one
    assert again["id"] == down["id"] and (again["step"], again["text"], again["verdict"]) == (1, "second step", "up")
    assert [(flag["step"], flag["text"]) for flag in manager.status()["taskFlags"]] == [(1, "second step")]


RULES = {"stale_days": 7, "done_step_days": 21, "trash_days": 7}


def test_entries_carry_their_added_time_and_can_be_reworded(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    tasks = memory.apply([TaskOp(op="add", title="Work on the paper", steps=[StepSpec(text="Draft the intro"), StepSpec(text="Fix the refs")])],
                         now="2026-10-20T10:00:00Z", session_id="s", call_id="c1", cursor=Cursor(), **RULES)
    assert [step.added_at for step in tasks[0].steps] == ["2026-10-20T10:00:00Z"] * 2
    assert "1. [ ] Draft the intro (added 2026-10-20)" in task_lines(tasks, now="2026-10-21T10:00:00Z")
    tasks = memory.apply([TaskOp(op="update", task_id="t001", reword=[RewordSpec(step=1, text="Write the introduction")], remove=[2])],
                         now="2026-10-21T10:00:00Z", session_id="s", call_id="c2", cursor=Cursor(), **RULES)
    assert [(step.text, step.added_at) for step in tasks[0].steps if not step.deleted_at] == [("Write the introduction", "2026-10-20T10:00:00Z")]
    # A reword passes the checks an add does: the length, the filler word, a text another entry has; and the number must exist.
    listed = [Task(id="t001", title="Work on the paper", steps=[Step(text="Write the introduction"), Step(text="Done one", done_at="2026-10-21T09:00:00Z")])]
    assert check({"name": "update_task_checklist", "arguments": {"task_id": "t001", "reword": [{"step": 1, "text": "Write the introduction"}]}}, listed)[1] == "Accepted."
    assert check({"name": "update_task_checklist", "arguments": {"task_id": "t001", "reword": [{"step": 1, "text": "done one"}]}}, listed)[1] == 'Rejected. "done one" is already on t001 (done).'
    assert check({"name": "update_task_checklist", "arguments": {"task_id": "t001", "reword": [{"step": 3, "text": "Nope"}]}}, listed)[1] == "Rejected. There is no entry 3 on t001."
    assert check({"name": "update_task_checklist", "arguments": {"task_id": "t001", "reword": [{"step": 2, "text": "Done one, again"}]}}, listed)[1] == "Rejected. Entry 2 of t001 is done; only an open entry can be reworded."
    kept = memory.apply([TaskOp(op="update", task_id="t001", steps=[StepSpec(text="Finished", done=True)])], now="2026-10-21T11:00:00Z", session_id="s", call_id="c3", cursor=Cursor(), **RULES)
    done_number = next(number for number, step in enumerate(kept[0].steps, start=1) if step.done_at)
    kept = memory.apply([TaskOp(op="update", task_id="t001", reword=[RewordSpec(step=done_number, text="Rewritten")])], now="2026-10-21T12:00:00Z", session_id="operator", call_id="o1", cursor=None, **RULES)
    assert "Finished" in [step.text for step in kept[0].steps] and "Rewritten" not in [step.text for step in kept[0].steps]  # the store holds the rule too
    assert normalize_op({"op": "update", "task_id": "t001", "reword": [{"step": "2", "text": " x "}, {"step": "no", "text": "y"}]}).reword == [RewordSpec(step=2, text="x")]


class FakeEmbedder:
    """Vectors by hand: texts about the search tool share a direction, the shipped one a little off it; the rest are orthogonal.
    Like the real client, one request record per batch of texts not seen before."""

    def __init__(self):
        self.calls = []
        self.seen = set()

    async def embed(self, texts):
        import numpy as np
        misses = [text for text in dict.fromkeys(texts) if text not in self.seen]
        if misses:
            self.calls.append({"request": {"model": "fake-embed", "input": misses}, "response": {"model": "fake-embed", "usage": {"prompt_tokens": len(misses)}}})
            self.seen.update(misses)
        return [np.array([1.0, 0.3] if "ship" in t.lower() else [1.0, 0.0] if "search" in t.lower() else [0.0, 1.0]) / (1.0 if "ship" not in t.lower() else 1.0440306508910551)
                for t in texts]


async def test_an_add_close_to_an_entry_is_held_back_once_and_the_updater_sees_the_earlier_worklog(tmp_path):
    memory = MemoryStore(tmp_path / "data").create()
    paths = SessionPaths(tmp_path / "data" / "sessions" / "s1").create()
    seeded = memory.apply([TaskOp(op="add", title="Work on the app", steps=[StepSpec(text="Ship the history search tool")])],
                          now="2026-09-23T01:00:00Z", session_id="s", call_id="c0", cursor=Cursor(), **RULES)
    memory.stream.emit("tasks_edited", {"ops": [], "tasks": [task.wire() for task in seeded]}, ts="2026-09-23T01:00:00Z", session_id="s1")  # the list reaches the stream
    for index, text in enumerate(("Edited Table 2 of the paper.", "Replied to Alice in Slack.")):
        memory.stream.emit("summary_written", line(index, text).wire(), ts=line(index, text).ts, session_id="s1")
    calls = [("update_task_checklist", {"task_id": "t001", "add": [{"text": "Add a search tool for the history"}], "evidence": ["01:10"]})]
    client = FakeClient([calls, calls, "Nothing new."])
    updater = BeliefUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1", embedder=FakeEmbedder())
    update = await updater.tick(now="2026-09-23T01:20:00Z")
    # The first call is held back with its lookalike; the same call again is applied: two rounds, one op, no rejection.
    assert update.rounds == 2 and [op.op for op in update.ops] == ["update"] and update.rejected == []
    assert [step.text for step in update.tasks[0].steps] == ["Ship the history search tool", "Add a search tool for the history"]
    assert update.warned == [('Warning, not applied: "Add a search tool for the history" is close to entry 1 of t001 "Ship the history search tool" (open since 2026-09-23). '
                              "If it is the same work, consider merging/rewording the open entr(ies); if it is truly a different work, call again unchanged and it will be applied.")]
    assert client.calls[1]["messages"][3] == {"role": "tool", "tool_call_id": "call_0", "content": update.warned[0]}
    assert [(call.round, call.applied, call.result) for call in update.calls] == [(1, False, update.warned[0]), (2, True, "Accepted.")]  # the log, for the card
    assert update.calls[0].op is not None and update.calls[0].op.steps[0].text == "Add a search tool for the history" and update.calls[0].tool == "update_task_checklist"
    # The one embeddings request (the second round's texts were cached) is on the record and logged like a model call.
    assert update.embedding["model"] == "fake-embed" and update.embedding["usage"]["input_tokens"] == 3 and {entry["model"] for entry in updater.usage["usage"]["byModel"]} == {"fake-model", "fake-embed"}
    assert [row["kind"] for row in tail_jsonl(paths.model_calls, 5)][-3:] == ["belief", "belief", "embedding"]
    # The next tick shows the lines already folded in before the new one, as many as the setting says, for the arc of the work.
    later = Summary(ts="2026-09-23T01:13:00Z", window_start="2026-09-23T01:12:00Z", window_end="2026-09-23T01:13:00Z", summary="Pushed the search tool.")
    memory.stream.emit("summary_written", later.wire(), ts=later.ts, session_id="s1")
    client = FakeClient(["Nothing new."])
    again = BeliefUpdater(paths, client, fake_settings(belief_context_summaries=1), memory, stream=memory.stream, session_id="s1")
    await again.tick(now="2026-09-23T01:30:00Z")
    prompt = client.calls[0]["messages"][1]["content"]
    earlier, new = prompt.split("</earlier_worklog>")[0], prompt.split("<worklog>")[1]
    assert "<earlier_worklog>\n- " in earlier and "Replied to Alice" in earlier and "Edited Table 2" not in prompt
    assert "Pushed the search tool." in new and "Replied to Alice" not in new and "<earlier_worklog>: the worklog lines before" in client.calls[0]["messages"][0]["content"]


async def test_a_belief_row_carries_the_list_only_when_its_ops_would_not_rebuild_it(tmp_path):
    from app.live.storage.sessions import tasks_left
    memory = MemoryStore(tmp_path / "data").create()
    paths = SessionPaths(tmp_path / "data" / "sessions" / "s1").create()
    memory.stream.emit("summary_written", line(0, "Edited Table 2.").wire(), ts=line(0, "x").ts, session_id="s1")
    client = FakeClient([[("add_task", {"title": "Finish Table 2 of the paper", "steps": [{"text": "collect numbers"}], "evidence": ["01:10"]})],
                         [("toggle_task_status", {"task_id": "t001", "step": 1, "done": True})], "Nothing new.", "Nothing new."])
    updater = BeliefUpdater(paths, client, fake_settings(), memory, stream=memory.stream, session_id="s1")
    first = await updater.tick(now="2026-09-23T01:20:00Z")
    assert first.tasks is not None and first.ops[0].task_id == "t001"  # a session's first row carries the list; the add's op names the task it made
    memory.stream.emit("summary_written", line(1, "Filled the table.").wire(), ts=line(1, "x").ts, session_id="s1")
    second = await updater.tick(now="2026-09-23T01:25:00Z")
    assert second.tasks is None and [op.op for op in second.ops] == ["status"]  # its op rebuilds the list from the first row's
    rows, _ = read_jsonl(paths.beliefs)
    assert "tasks" not in rows[-1] and [(s.text, s.done_at) for s in tasks_left(rows, "s1")[0].steps] == [("collect numbers", "2026-09-23T01:25:00Z")]
    window = memory.stream.window_at("2026-09-23T01:25:00Z", "2026-09-23T01:26:00Z", "s1", lookback_days=30)
    assert [t.wire() for t in window.tasks()] == [t.wire() for t in memory.tasks()] and memory.stream.events()[-1].data.get("tasks") is None
    # An operator edit between ticks is not among the row's ops: the next row carries the list again. So does a rule firing.
    memory.apply([TaskOp(op="rename", task_id="t001", title="Finish Table 2")], now="2026-09-23T01:27:00Z", session_id="operator", call_id="op1", cursor=None, **RULES)
    memory.stream.emit("tasks_edited", {"ops": [], "tasks": [t.wire() for t in memory.tasks()]}, ts="2026-09-23T01:27:00Z", session_id="s1")
    memory.stream.emit("summary_written", line(2, "Sent it.").wire(), ts=line(2, "x").ts, session_id="s1")
    third = await updater.tick(now="2026-09-23T01:30:00Z")
    assert third.ops == [] and third.tasks is not None and third.tasks[0].title == "Finish Table 2"
    later = Summary(ts="2026-11-23T01:13:00Z", window_start="2026-11-23T01:03:00Z", window_end="2026-11-23T01:13:00Z", summary="Back after weeks.")
    memory.stream.emit("summary_written", later.wire(), ts=later.ts, session_id="s1")
    fourth = await updater.tick(now="2026-11-23T01:20:00Z")
    assert fourth.ops == [] and fourth.tasks is not None and fourth.tasks[0].status == "paused"  # the stale pause: nothing of the model's
    assert [t.wire() for t in tasks_left(read_jsonl(paths.beliefs)[0], "s1")] == [t.wire() for t in memory.tasks()]
