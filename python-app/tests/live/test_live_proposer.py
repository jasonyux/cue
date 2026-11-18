"""Proposer: the proposer, its record shape, the truncation fallback, and notification."""



import pytest
from live_helpers import TRUNCATED, FakeClient, fake_settings

from app.live.observer.recorder import record as record_row
from app.live.observer.summarize import Summarizer
from app.live.proposer.feedback_digest import input_images, input_text, proposer_request
from app.live.proposer.notify import notify_proposals
from app.live.proposer.prompts import REPLY_CHARS, chat_lines
from app.live.proposer.propose import Proposer, RankedProposalsStrategy
from app.live.storage.paths import SessionPaths
from app.live.storage.records import Decision, Proposal, ProposalSet, Summary
from app.live.storage.search import HistoryIndex
from app.live.storage.stream import Stream, recent_decisions
from app.live.utils.embed import unit
from app.live.utils.jsonl import append_jsonl, tail_jsonl


async def test_summarizer_and_proposer_ticks_record_everything(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    settings = fake_settings(proposer_max_tokens=4096)  # the summarizer keeps the client's automatic budget
    proposal = ('{"summary": "Still on the thread.", "proposals": ['
                '{"kind": "act", "action": "propose", "task": "Find the deck and share it with Alice", "reason": "asked twice", '
                '"reversible": false, "confidence": 4}, '
                '{"kind": "lookup", "action": "do", "task": "Look up where the Q3 deck lives", "reason": "unsure", "confidence": 2}]}')
    client = FakeClient(['{"summary": "Read the Slack thread with Alice about the deck."}', proposal, "not json at all"])
    usage = {}
    stream = Stream(tmp_path / "stream")
    summarizer = Summarizer(paths, client, settings, stream=stream, session_id="s", usage=usage)
    proposer = Proposer(paths, client, settings, stream=stream, session_id="s", usage=usage)
    assert await summarizer.tick() is None and await proposer.tick() is None
    record_row(paths, stream, {"kind": "observer_started", "ts": "2026-09-17T10:00:00Z"})
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Slack", "window": "Alice",
                           "tree": "Slack · window\n[0] AXTextArea value='where is the deck?'"})
    summary = await summarizer.tick(now="2026-09-17T10:01:00Z")
    assert summary.summary.startswith("Read the Slack thread") and client.calls[0]["thinking"] is False
    proposal_set = await proposer.tick(now="2026-09-17T10:05:00Z")
    assert [(event.kind, event.call_id) for event in stream.events()][2:] == [("summary_written", summary.call_id), ("proposals_made", proposal_set.call_id)]
    assert [event.id for event in stream.events()][2:] == [summary.id, proposal_set.id] and len(summary.id) == 12 and summary.id != proposal_set.id
    assert tail_jsonl(paths.summaries, 1)[0]["id"] == summary.id and tail_jsonl(paths.proposals, 1)[0]["id"] == proposal_set.id  # the rows carry it too
    assert proposal_set.action == "propose" and proposal_set.task.startswith("Find the deck") and proposal_set.reasoning
    assert proposal_set.kind == "act" and [item.kind for item in proposal_set.proposals] == ["act", "lookup"]
    assert proposal_set.proposals[1].task.startswith("Look up") and "action" not in proposal_set.proposals[1].wire()
    assert client.calls[1]["thinking"] is True
    assert client.calls[0]["max_tokens"] is None and client.calls[1]["max_tokens"] == 4096
    proposer_system_prompt, proposer_prompt = (message["content"] for message in client.calls[1]["messages"])
    assert "Read the Slack thread" in proposer_prompt and "<current_window>" in proposer_prompt and "where is the deck" in proposer_prompt
    assert "up to 3 proposals" in proposer_system_prompt and '"explain"' in proposer_system_prompt and "explained simply" in proposer_system_prompt
    assert "pending checklist entry" in proposer_system_prompt
    calls = tail_jsonl(paths.model_calls, 10)
    assert [item["kind"] for item in calls] == ["summary", "proposer"] and calls[1]["response"]["choices"]
    assert usage["usage"]["calls"] == 2 and usage["usage"]["byModel"][0]["model"] == "fake-model"
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:06:00Z", "app": "Finder", "window": "", "tree": "x"})
    stream.emit("run_started", {"ts": "2026-09-17T10:06:30Z", "runId": "r1", "event": "started", "prompt": "Find the deck and share it with Alice"},
                ts="2026-09-17T10:06:30Z", session_id="s")  # a run in progress: read from the stream, not handed in
    stream.emit("chat_message", {"ts": "2026-09-17T10:06:40Z", "runId": "c0", "chatId": "c1", "turn": 1, "agent": "claude", "role": "user", "text": "Find the hotel"},
                ts="2026-09-17T10:06:40Z", session_id="s")
    stream.emit("chat_reply", {"ts": "2026-09-17T10:06:50Z", "runId": "c0", "chatId": "c1", "turn": 1, "agent": "claude", "role": "agent", "text": "Found it.", "status": "completed"},
                ts="2026-09-17T10:06:50Z", session_id="s")
    stream.emit("chat_message", {"ts": "2026-09-17T10:07:00Z", "runId": "c1", "chatId": "c1", "turn": 2, "agent": "claude", "role": "user", "text": "Book the Sydney flight"},
                ts="2026-09-17T10:07:00Z", session_id="s")
    stream.emit("chat_reply", {"ts": "2026-09-17T10:08:00Z", "runId": "c1", "chatId": "c1", "turn": 1, "agent": "claude", "role": "agent", "text": "Booked for Dec 3.", "status": "completed"},
                ts="2026-09-17T10:08:00Z", session_id="s")
    fallback = await proposer.tick(now="2026-09-17T10:10:00Z")
    assert fallback.action == "none" and "not a JSON object" in fallback.summary and fallback.raw == "not json at all"
    busy_prompt = client.calls[2]["messages"][1]["content"]
    assert "<running>\n- Find the deck and share it with Alice\n</running>" in busy_prompt
    assert "<running>" not in client.calls[1]["messages"][1]["content"]
    assert "asked: Book the Sydney flight" in busy_prompt and "answered (completed): Booked for Dec 3." in busy_prompt  # the chat, so it is not proposed again
    assert "Find the hotel" not in busy_prompt  # the latest turn only
    long_turn = chat_lines([{"id": "c9", "ts": "2026-09-17T10:07:00Z", "text": "q" * 2500, "reply": "r" * 2400 + "end", "status": "completed"}])
    assert long_turn.count("q") == 2500 and long_turn.endswith("…" + "r" * (REPLY_CHARS - 3) + "end")  # the question whole, the reply's end kept
    assert "asked:" not in client.calls[1]["messages"][1]["content"]
    assert fallback.proposals == [] and fallback.task is None
    stored = tail_jsonl(paths.proposals, 10)
    assert len(stored) == 2 and len(tail_jsonl(paths.summaries, 10)) == 1
    assert stored[0]["proposals"][1] == {"kind": "lookup", "task": "Look up where the Q3 deck lives", "reason": "unsure", "confidence": 2, "evidence": []}
    assert stored[1]["task"] is None and "reasoning" in stored[0] and "fallback" not in stored[0]


async def test_proposer_input_is_worklog_lines_then_raw_rows_and_a_strategy_picks(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    stream = Stream(tmp_path / "stream")
    stream.emit("summary_written", Summary(ts="2026-09-17T09:59:00Z", window_start="2026-09-17T09:58:00Z",
                                   window_end="2026-09-17T09:59:00Z", summary="Earlier, in Mail.").wire(), ts="2026-09-17T09:59:00Z", session_id="s")
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Slack", "window": "w", "tree": "[0] AXWindow slack-tree"})
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:10Z", "app": "Notes", "window": "n", "tree": "[0] AXWindow notes-tree"})
    seen = []

    class Recording(RankedProposalsStrategy):
        def messages(self, inputs):
            seen.append(inputs)
            return super().messages(inputs)

    def proposer(shown):
        client = FakeClient(['{"summary": "s", "proposals": []}'])
        return client, Proposer(paths, client, fake_settings(proposer_inputs=shown), stream=stream, session_id="s", strategy=Recording())

    client, full = proposer(("worklog", "snapshots", "current_window"))
    await full.tick(now="2026-09-17T10:05:00Z")
    inputs, prompt = seen[-1], client.calls[0]["messages"][1]["content"]
    assert sorted(inputs.input) == ["current_window", "snapshots", "worklog"]
    assert [item.summary for item in inputs.input["worklog"]] == ["Earlier, in Mail."]
    assert [s.app for s in inputs.input["snapshots"]] == ["Slack", "Notes"] and inputs.input["current_window"].app == "Notes"
    assert inputs.window_start == "2026-09-17T10:00:00Z" and prompt.count("notes-tree") == 2  # in the digest and as the current window

    # The proposerInputs setting (proposer.inputs) decides which keys exist; the strategy renders each key it finds.
    for shown in (("worklog",), ("snapshots",), ("current_window",)):
        worklog_in, digest_in, window_in = (key in shown for key in ("worklog", "snapshots", "current_window"))
        client, ablated = proposer(shown)
        await ablated.tick(now="2026-09-17T10:05:00Z")
        inputs, prompt, system = seen[-1], client.calls[0]["messages"][1]["content"], client.calls[0]["messages"][0]["content"]
        assert tuple(inputs.input) == shown
        assert ("<worklog>" in prompt) is worklog_in and ("<observations>" in prompt) is digest_in
        assert ("<current_window>" in prompt) is window_in and prompt.count("notes-tree") == int(digest_in) + int(window_in)
        assert ("worklog lines" in system) is worklog_in and ("current window's accessibility tree" in system) is window_in


async def test_truncated_reasoning_falls_back_to_a_plain_answer(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    stream = Stream(tmp_path / "stream")
    client = FakeClient([TRUNCATED, '{"summary": "Debugging.", "proposals": [{"kind": "explain", "task": "Explain the CUDA error"}]}',
                         '{"summary": "Read the traceback."}'])
    usage = {}
    proposer = Proposer(paths, client, fake_settings(proposer_thinking=True), stream=stream, session_id="s", usage=usage)
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-21T20:15:00Z", "app": "Code", "window": "train.py", "tree": "x"})
    proposal_set = await proposer.tick(now="2026-09-21T20:15:33Z")
    assert proposal_set.task == "Explain the CUDA error" and proposal_set.fallback == "thinking_truncated"
    assert [call["thinking"] for call in client.calls] == [True, False] and client.calls[0]["messages"] == client.calls[1]["messages"]
    assert [item["kind"] for item in tail_jsonl(paths.model_calls, 10)] == ["proposer_truncated", "proposer"]
    assert usage["usage"]["calls"] == 2  # the wasted attempt still counts
    summarizer = Summarizer(paths, client, fake_settings(summary_thinking=False), stream=stream, session_id="s")
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-21T20:16:00Z", "app": "Code", "window": "train.py", "tree": "y"})
    summary = await summarizer.tick(now="2026-09-21T20:16:30Z")
    assert summary.summary == "Read the traceback." and summary.fallback is None and len(client.calls) == 3


def test_proposal_set_from_model_output_is_defensive():
    legacy = ProposalSet.from_model_output({"action": "DO", "confidence": "9", "task": "  x "})  # single-task shape still parses
    assert legacy.action == "propose" and legacy.task == "x" and legacy.confidence == 5.0 and legacy.kind == "act"  # clamped to 1-5
    assert legacy.proposals == [Proposal(kind="act", task="x", reason="", confidence=5.0)]
    assert ProposalSet.from_model_output({"action": "none", "task": "ignored"}).proposals == []
    empty = ProposalSet.from_model_output({"action": "explode", "confidence": "nope"})
    assert (empty.summary, empty.kind, empty.action, empty.task, empty.reason, empty.confidence, empty.proposals) == (
        "(no summary)", "act", "none", None, "", 0.0, [])
    assert empty.wire()["task"] is None  # the console and older files expect the flat task, even when empty
    ranked = ProposalSet.from_model_output({"summary": "s", "proposals": [
        {"kind": "explain", "action": "do", "task": "Explain the traceback", "reason": "error on screen", "confidence": 4},
        {"kind": "weird", "task": "Explain the traceback"},  # duplicate task is dropped
        {"kind": "lookup", "task": "Find the docs for asyncio.wait_for"},
        {"kind": "act", "task": ""},  # no task: dropped
        "not a dict",
        {"kind": "act", "task": "Draft the reply"},
    ]}, limit=2)
    assert [item.task for item in ranked.proposals] == ["Explain the traceback", "Find the docs for asyncio.wait_for"]
    assert ranked.kind == "explain" and ranked.action == "propose" and ranked.task == "Explain the traceback"
    assert ranked.proposals[0].wire() == {"kind": "explain", "task": "Explain the traceback", "reason": "error on screen", "confidence": 4, "evidence": []}
    assert ranked.proposals[1].wire() == {"kind": "lookup", "task": "Find the docs for asyncio.wait_for", "reason": "", "confidence": 0.0, "evidence": []}
    assert ProposalSet.from_model_output({"proposals": [{"kind": "nope", "task": "t"}]}).proposals[0].kind == "act"
    assert len(ProposalSet.from_model_output({"proposals": [{"task": str(i)} for i in range(9)]}, limit=99).proposals) == 5
    # Stored records from before ranked proposals (and from failed ticks) read back in the same shape.
    stored = ProposalSet.parse_stored({"ts": "t", "action": "propose", "task": "Total the sheet", "reason": "r", "confidence": 3,
                                    "reversible": False, "policy": "downgraded_to_proposal"})
    assert [p.wire() for p in stored.proposals] == [{"kind": "act", "task": "Total the sheet", "reason": "r", "confidence": 3, "evidence": []}]


def test_proposals_carry_the_by_hand_estimate():
    """The proposer is asked for the person's own time on each proposal; a usable number is kept as humanSeconds."""
    from app.live.proposer.prompts import proposer_system

    assert '"human_seconds"' in proposer_system(3, ["tasks"])
    ranked = ProposalSet.from_model_output({"summary": "s", "proposals": [
        {"task": "Draft the reply", "human_seconds": 900},
        {"task": "Total the sheet", "human_seconds": "120"},  # a numeric string still counts
        {"task": "Find the docs", "human_seconds": "a while"},  # not a number: none
        {"task": "Rename the files", "human_seconds": -5},  # not positive: none
        {"task": "Book the room"},  # missing: none
    ]}, limit=5)
    assert [item.human_seconds for item in ranked.proposals] == [900.0, 120.0, None, None, None]
    assert ranked.proposals[0].wire()["humanSeconds"] == 900.0 and "humanSeconds" not in ranked.proposals[4].wire()
    assert "humanSeconds" not in ranked.wire()["proposals"][4]  # nested in the stored set too: older readers see the old shape
    stored = ProposalSet.parse_stored(ranked.wire())  # read back from proposals.jsonl
    assert stored.proposals[0].human_seconds == 900.0
    assert "reversible" not in stored.wire()
    assert ProposalSet.parse_stored({"ts": "t", "error": "boom"}).error == "boom"


def test_policy_records_and_notifies():
    settings = fake_settings(notify=True)
    sent = []

    def notifier(*args):
        sent.append(args)
        return True

    none = notify_proposals(ProposalSet(action="none"), settings, notifier=notifier)
    assert none.effect == "recorded" and sent == []
    done = notify_proposals(ProposalSet(action="propose", task="Draft the reply"), settings, notifier=notifier)
    assert done.effect == "notified" and sent[0][1] == "[act] Draft the reply"
    ranked = notify_proposals(ProposalSet(kind="explain", action="propose", task="Explain it", proposals=[Proposal(task=t) for t in "abc"]),
                          settings, notifier=notifier)
    assert ranked.effect == "notified" and sent[1][1] == "[explain] Explain it (+2 more)"
    quiet = notify_proposals(ProposalSet(action="propose", task="x"), fake_settings(notify=False), notifier=lambda *a: True)
    assert quiet.effect == "proposed"


def test_window_folds_decisions_with_their_digest_and_reason(tmp_path):
    stream = Stream(tmp_path / "stream")
    for number, verdict in enumerate(["accept", "reject", "accept", "accept", "reject"]):
        ts = f"2026-09-28T10:0{number}:00Z"
        stream.emit("proposal_accepted" if verdict == "accept" else "proposal_rejected", {"ts": ts, "proposalsTs": ts, "decision": verdict, "kind": "act", "task": f"t{number}"}, ts=ts, session_id="s")
    stream.emit("decision_digested", {"proposalsTs": "2026-09-28T10:01:00Z", "decisionTs": "2026-09-28T10:01:00Z", "context": "in Mail"},
                ts="2026-09-28T10:01:05Z", session_id="s")
    stream.emit("reject_reason_given", {"proposalsTs": "2026-09-28T10:04:00Z", "decisionTs": "2026-09-28T10:04:00Z", "reason": "not now", "typed": False},
                ts="2026-09-28T10:04:05Z", session_id="s")
    window = stream.window_at("2026-09-28T10:05:00Z", "2026-09-28T10:10:00Z", "other-session", lookback_days=30)  # decisions: every session
    assert [(d.decision, d.task, d.context, d.reject_reason) for d in window.decisions(2, 2)] == [
        ("reject", "t1", "in Mail", ""), ("accept", "t2", "", ""), ("accept", "t3", "", ""), ("reject", "t4", "", "not now")]
    earlier = stream.window_at("2026-09-28T10:00:00Z", "2026-09-28T10:04:00Z", "s", lookback_days=30)  # strictly before the end: t4 not yet
    assert [d.task for d in earlier.decisions(5, 5)] == ["t0", "t1", "t2", "t3"]


def test_the_feedback_window_prefers_decisions_the_person_typed_a_reason_for():
    def decision(number, verdict, reason="", typed=False):
        return Decision(ts=f"2026-09-28T10:{number:02d}:00Z", decision=verdict, task=f"t{number}", reject_reason=reason, reason_typed=typed)
    decisions = [decision(0, "reject", "already fixed it myself", True), decision(1, "reject"), decision(2, "reject", "do it myself"),  # 2: a preset
                 decision(3, "accept"), decision(4, "reject", "nah man", True), decision(5, "reject", "I am already working on it", True),
                 decision(6, "accept"), decision(7, "reject")]  # 4: typed, but under TYPED_MIN_WORDS: ranks like a click
    # Rejections: the two worded ones, however old, then the latest other; accepts carry no reason: the latest N.
    assert [d.task for d in recent_decisions(decisions, 3, 3)] == ["t0", "t3", "t5", "t6", "t7"]
    assert [d.task for d in recent_decisions(decisions, 1, 1)] == ["t5", "t6"]  # the latest typed rejection fills the window alone
    assert [d.task for d in recent_decisions(decisions, 6, 6)] == ["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    assert [d.task for d in recent_decisions(decisions[::-1], 3, 3)] == ["t0", "t3", "t5", "t6", "t7"]  # the input order does not matter
    assert [d.task for d in recent_decisions(decisions, 2, 1)] == ["t0", "t5", "t6"]  # each verdict its own count


def test_window_lists_undecided_rounds_until_answered(tmp_path):
    stream = Stream(tmp_path / "stream")
    for ts, task in (("2026-09-28T10:00:00Z", "Older idea"), ("2026-09-28T10:05:00Z", "Decided idea"), ("2026-09-28T10:10:00Z", "Newest idea")):
        stream.emit("proposals_made", {"ts": ts, "summary": "s", "action": "propose", "task": task,
                                     "proposals": [{"kind": "lookup", "task": task}, {"kind": "act", "task": f"{task} (alt)"}]}, ts=ts, session_id="s")
    stream.emit("proposals_made", {"ts": "2026-09-28T10:11:00Z", "error": "boom"}, ts="2026-09-28T10:11:00Z", session_id="s")
    stream.emit("proposal_rejected", {"ts": "2026-09-28T10:06:00Z", "proposalsTs": "2026-09-28T10:05:00Z", "decision": "reject"},
                ts="2026-09-28T10:06:00Z", session_id="s")
    pending = stream.window_at("2026-09-28T10:12:00Z", "2026-09-28T10:15:00Z", "s", lookback_days=30).undecided(1)
    assert [(item.decision, item.kind, item.task) for item in pending] == [("", "lookup", "Newest idea")]  # the top proposal only
    assert [item.task for item in stream.window_at("2026-09-28T10:00:00Z", "2026-09-28T10:05:00Z", "s", lookback_days=30).undecided(5)] == ["Older idea"]


async def test_proposer_is_shown_decisions_when_the_source_is_on(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    stream = Stream(tmp_path / "stream")
    settings = fake_settings(proposer_inputs=("tasks", "feedback"))
    client = FakeClient(['{"summary": "s", "proposals": []}'])
    proposer = Proposer(paths, client, settings, stream=stream, session_id="b", usage={})
    stream.emit("proposal_rejected", {"ts": "2026-09-28T19:51:15Z", "proposalsTs": "p1", "decision": "reject", "kind": "explain", "task": "Explain the form"},
                ts="2026-09-28T19:51:15Z", session_id="a")
    stream.emit("decision_digested", {"proposalsTs": "p1", "decisionTs": "2026-09-28T19:51:15Z", "context": "Filling a visa form in Vivaldi"},
                ts="2026-09-28T19:51:20Z", session_id="a")
    stream.emit("proposal_rejected", {"ts": "2026-09-28T19:55:00Z", "proposalsTs": "p2", "decision": "reject", "kind": "lookup", "task": "Find the docs"},
                ts="2026-09-28T19:55:00Z", session_id="a")
    stream.emit("reject_reason_given", {"proposalsTs": "p2", "decisionTs": "2026-09-28T19:55:00Z", "reason": "do it myself", "typed": False},
                ts="2026-09-28T19:55:05Z", session_id="a")
    stream.emit("proposals_made", {"ts": "2026-09-28T20:00:00Z", "action": "propose", "proposals": [{"kind": "act", "task": "Draft the reply"}]},
                ts="2026-09-28T20:00:00Z", session_id="b")
    await proposer.run_window(stream.window_at("2026-09-28T20:04:00Z", "2026-09-28T20:05:00Z", "b", lookback_days=30))
    system, prompt = (message["content"] for message in client.calls[0]["messages"])
    assert "recent decisions on earlier proposals" in system and "Learn their preferences from them" in system
    assert "rejected (explain): Explain the form · while: Filling a visa form in Vivaldi" in prompt
    assert "proposed, no decision yet (act): Draft the reply" in prompt and "Worklog" not in prompt
    assert "rejected, do it myself (lookup): Find the docs" in prompt


def test_feedback_digest_gets_the_proposer_input_text_and_its_screenshots(tmp_path):
    from PIL import Image

    paths = SessionPaths(tmp_path / "s").create()
    for name in ("a.png", "b.png", "c.png"):
        Image.new("RGB", (40, 20), "white").save(paths.screenshots / name)
    for ts, name in (("2026-09-28T10:01:00Z", "a.png"), ("2026-09-28T10:03:00Z", "b.png"), ("2026-09-28T10:09:00Z", "c.png")):
        append_jsonl(paths.snapshots, {"kind": "screenshot_taken", "ts": ts, "app": "Code", "window": "w", "path": name})
    logged = {"ts": "2026-09-28T10:05:00Z", "kind": "proposer", "windowStart": "2026-09-28T10:00:00Z", "windowEnd": "2026-09-28T10:05:00Z",
              "request": {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": [
                  {"type": "text", "text": "Worklog, oldest first:\n- edited prompts.py\n\nAttached: 2 screenshot(s).\n\nReturn the JSON object."},
                  {"type": "image_url", "image_url": {"url": "<png image, 999 base64 chars omitted>"}},
                  {"type": "image_url", "image_url": {"url": "<png image, 999 base64 chars omitted>"}}]}]}, "response": {}}
    append_jsonl(paths.model_calls, logged)
    call = proposer_request(paths, "2026-09-28T10:05:00Z")
    assert input_text(call) == "Worklog, oldest first:\n- edited prompts.py\n\nAttached: 2 screenshot(s)."  # the closing instruction is not input
    images = input_images(call, paths, 32)
    assert len(images) == 2 and all(url.startswith("data:image/png;base64,") for url in images)  # a and b: in the window, newest two
    assert input_images({**call, "request": {"messages": [{"role": "user", "content": "text only"}]}}, paths, 32) == []
    with pytest.raises(Exception, match="No logged proposer request"):
        proposer_request(paths, "2026-09-28T10:06:00Z")


# ----- the duplicate filter ---------------------------------------------------------------------------------------

DIRECTIONS = {"deck": [1.0, 0.0, 0.0, 0.0], "tests": [0.0, 1.0, 0.0, 0.0], "letter": [0.0, 0.0, 1.0, 0.0], "visa": [0.0, 0.0, 0.0, 1.0]}


class FakeEmbedder:
    """Vectors by hand: texts that share a direction are near-duplicates, everything else is orthogonal."""

    def __init__(self):
        self.calls = []  # like the real client: one request record per batch of texts not yet seen, none when all are cached
        self.embedded = []
        self.seen = set()

    async def embed(self, texts):
        self.embedded.append(list(texts))
        misses = [text for text in dict.fromkeys(texts) if text not in self.seen]
        if misses:
            self.calls.append({"request": {"model": "fake-embed", "input": misses}, "response": {"model": "fake-embed", "usage": {"prompt_tokens": len(misses)}}})
            self.seen.update(misses)
        vectors = []
        for text in texts:
            key = next(k for k in DIRECTIONS if k in text.lower())
            tilt = 0.5 if "(similar)" in text else 0.0  # ~0.89 against the plain direction: a rephrasing
            base = DIRECTIONS[key]
            other = DIRECTIONS["tests" if key != "tests" else "deck"]
            vectors.append(unit([b + tilt * o for b, o in zip(base, other, strict=True)]))
        return vectors

    async def close(self):
        pass


def feedback_row(ts, decision, task, reason=""):
    return {"ts": ts, "sessionId": "old", "decision": decision, "kind": "act", "task": task, "rejectReason": reason, "reasonTyped": False, "callId": "c"}


async def test_the_duplicate_filter_drops_what_was_decided_or_already_shown(tmp_path):
    from app.live.proposer.dedup import filter_duplicates, references
    feedback = tmp_path / "feedback.jsonl"
    now = "2026-10-05T12:00:00Z"
    for row in (feedback_row("2026-09-01T09:00:00Z", "accept", "Share the deck with Alice"),            # decided long ago: still a reference
                feedback_row("2026-10-05T10:00:00Z", "reject", "Run the tests", "not now"),             # not now, 2 h ago: a reference
                feedback_row("2026-10-03T10:00:00Z", "reject", "File the visa application", "not now")):  # not now, 50 h ago: expired
        append_jsonl(feedback, row)
    stream = Stream(tmp_path / "stream")
    stream.emit("proposals_made", {"ts": "2026-10-05T11:30:00Z", "proposals": [{"kind": "act", "task": "Draft the visa cover letter"}]},
                ts="2026-10-05T11:30:00Z", session_id="s")   # this session, 30 min ago
    stream.emit("proposals_made", {"ts": "2026-10-03T11:30:00Z", "proposals": [{"kind": "act", "task": "Book the visa appointment"}]},
                ts="2026-10-03T11:30:00Z", session_id="s")   # this session, too old
    stream.emit("proposals_made", {"ts": "2026-10-05T11:40:00Z", "proposals": [{"kind": "act", "task": "Rebuild the deck index"}]},
                ts="2026-10-05T11:40:00Z", session_id="other")  # another session: not this session's references
    settings = fake_settings(proposer_dedup=True, proposer_dedup_not_now_hours=24, proposer_dedup_session_hours=24)
    window = stream.window_at("2026-10-05T11:55:00Z", now, "s", lookback_days=30)
    refs = references(feedback, window, settings)
    assert [(r.text, r.source, r.note) for r in refs] == [
        ("Share the deck with Alice", "decision", "accepted 2026-09-01"),
        ("Run the tests", "not_now", "rejected, not now, 2026-10-05"),
        ("Draft the visa cover letter", "session", "proposed 07:30 this session")]
    proposals = ProposalSet.from_model_output({"summary": "s", "proposals": [
        {"kind": "act", "task": "Share the deck with Alice (similar)", "reason": "asked again", "confidence": 4},
        {"kind": "lookup", "task": "Run the tests (similar)", "confidence": 3},
        {"kind": "act", "task": "Book the visa appointment", "confidence": 2}]})
    embedder = FakeEmbedder()
    filtered = await filter_duplicates(proposals, refs, embedder, 0.8)
    # The two rephrasings are filtered with their scores and what they hit; the visa one is new (its old twin expired).
    assert [(f.task, f.score, f.matched, f.source) for f in filtered.filtered] == [
        ("Share the deck with Alice (similar)", 0.894, "Share the deck with Alice", "decision"),
        ("Run the tests (similar)", 0.894, "Run the tests", "not_now")]
    assert [p.task for p in filtered.proposals] == ["Book the visa appointment"]
    assert filtered.action == "propose" and filtered.task == "Book the visa appointment" and filtered.kind == "act" and filtered.confidence == 2
    assert embedder.embedded == [[p.task for p in proposals.proposals] + [r.text for r in refs]]  # one batch, proposals first
    # A stricter threshold keeps them; everything filtered leaves a set with no action.
    assert (await filter_duplicates(proposals, refs, embedder, 0.95)).filtered == []
    none_left = await filter_duplicates(proposals.model_copy(update={"proposals": proposals.proposals[:2]}), refs, embedder, 0.8)
    assert none_left.action == "none" and none_left.task is None and none_left.proposals == [] and len(none_left.filtered) == 2
    assert notify_proposals(none_left, settings, notifier=lambda *a: True).effect == "recorded"  # nothing to offer: no notification
    assert (await filter_duplicates(proposals, [], embedder, 0.8)) is proposals  # no references yet: untouched


async def test_a_proposer_tick_with_the_filter_on_records_the_filtered_proposals(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    feedback = tmp_path / "feedback.jsonl"
    append_jsonl(feedback, feedback_row("2026-09-17T09:00:00Z", "accept", "Share the deck with Alice"))
    settings = fake_settings(proposer_dedup=True, proposer_dedup_threshold=0.8)
    reply = ('{"summary": "Still on the thread.", "proposals": [{"kind": "act", "task": "Share the deck with Alice (similar)", "confidence": 4}, '
             '{"kind": "lookup", "task": "Run the tests", "confidence": 2}]}')
    client, stream, embedder = FakeClient([reply]), Stream(tmp_path / "stream"), FakeEmbedder()
    proposer = Proposer(paths, client, settings, stream=stream, session_id="s", embedder=embedder, feedback=feedback)
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Slack", "window": "Alice", "tree": "x"})
    proposal_set = await proposer.tick(now="2026-09-17T10:05:00Z")
    assert [p.task for p in proposal_set.proposals] == ["Run the tests"] and proposal_set.task == "Run the tests" and proposal_set.kind == "lookup"
    assert [(f.task, f.matched, f.note) for f in proposal_set.filtered] == [("Share the deck with Alice (similar)", "Share the deck with Alice", "accepted 2026-09-17")]
    stored = tail_jsonl(paths.proposals, 1)[0]
    assert stored["filtered"][0]["score"] == 0.894 and stored["filtered"][0]["source"] == "decision" and [p["task"] for p in stored["proposals"]] == ["Run the tests"]
    # The round's embeddings call is on the record for the cost panel and in the session's totals next to the proposer's own call.
    assert stored["embedding"] == {"model": "fake-embed", "usage": {"input_tokens": 3, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                                                                     "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 0}}
    assert [(entry["model"], entry["calls"], entry["inputTokens"]) for entry in proposer.usage["usage"]["byModel"]] == [("fake-embed", 1, 3), ("fake-model", 1, 100)]
    assert stream.events()[-1].data["filtered"][0]["task"].startswith("Share the deck")
    # The embeddings request the round made is logged like a model call, with the window's bounds.
    logged = [row for row in tail_jsonl(paths.model_calls, 10) if row["kind"] == "embedding"]
    assert len(logged) == 1 and logged[0]["windowEnd"] == "2026-09-17T10:05:00Z"
    assert logged[0]["request"]["input"] == ["Share the deck with Alice (similar)", "Run the tests", "Share the deck with Alice"]
    fresh = ProposalSet.from_model_output({"summary": "s", "proposals": [p.model_dump() for p in proposal_set.proposals + proposal_set.filtered]})
    cached = await proposer.dedup(fresh, stream.window_at("2026-09-17T10:05:00Z", "2026-09-17T10:06:00Z", "s", lookback_days=30))
    assert cached.embedding is None and len(embedder.calls) == 1  # every text cached: no request, nothing spent, nothing recorded
    off = Proposer(paths, FakeClient([reply]), fake_settings(), stream=stream, session_id="s")  # the filter off: no embedder needed
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:07:00Z", "app": "Slack", "window": "Alice", "tree": "y"})
    assert len((await off.tick(now="2026-09-17T10:10:00Z")).proposals) == 2


async def test_the_proposer_searches_the_history_when_the_model_asks_and_the_rounds_are_capped(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    stream = Stream(tmp_path / "stream")
    old = stream.emit("summary_written", Summary(ts="2026-09-10T09:00:00Z", window_start="2026-09-10T08:59:00Z", window_end="2026-09-10T09:00:00Z",
                                                 summary="Booked the Sydney hotel for NeurIPS on booking.com.").wire(), ts="2026-09-10T09:00:00Z", session_id="old")
    new = stream.emit("summary_written", Summary(ts="2026-10-19T14:59:00Z", window_start="2026-10-19T14:58:00Z", window_end="2026-10-19T14:59:00Z",
                                                 summary="Looking at flights for the conference.").wire(), ts="2026-10-19T14:59:00Z", session_id="s")
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-10-19T15:00:00Z", "app": "Safari", "window": "Google Flights", "tree": "[0] AXWindow flights"})
    declined = stream.emit("proposal_rejected", {"ts": "2026-10-01T09:30:00Z", "proposalsTs": "2026-10-01T09:25:00Z", "decision": "reject", "kind": "act", "task": "Book the Paris hotel"},
                           ts="2026-10-01T09:30:00Z", session_id="old")  # a decision from another session, within the lookback: shown with its id, so it can be cited
    proposal = ('{"summary": "Travel.", "proposals": [{"kind": "act", "task": "Find Sydney flights for NeurIPS, Dec 2 to 6", '
                f'"reason": "the hotel is booked", "confidence": 4, "evidence": ["{old.id}", "made-up", "{new.id}", "{old.id}", "{declined.id}"]}}]}}')
    client = FakeClient([[("search_history", {"query": "NeurIPS hotel"})], proposal])
    index = HistoryIndex(tmp_path / "memory" / "search.sqlite", stream)
    proposer = Proposer(paths, client, fake_settings(), stream=stream, session_id="s", index=index)
    made = await proposer.tick(now="2026-10-19T15:05:00Z")
    assert made.task.startswith("Find Sydney flights") and len(made.searches) == 1 and "found" not in made.searches[0]
    assert made.proposals[0].evidence == [old.id, new.id, declined.id]  # the hit, this session's line and the decision, each once; the made-up id dropped
    assert [(hit["id"], hit["kind"], hit["sessionId"], hit["text"][:20]) for hit in made.cited] == [
        (old.id, "summary_written", "old", "Booked the Sydney ho"), (declined.id, "decision", "old", "declined: Book the P")]  # what the console may not hold, with its text
    assert tail_jsonl(paths.proposals, 1)[0]["proposals"][0]["evidence"] == [old.id, new.id, declined.id] and tail_jsonl(paths.proposals, 1)[0]["cited"][0]["id"] == old.id
    assert made.searches[0] == {"query": "NeurIPS hotel", "regex": None, "kinds": ["worklog", "chat"], "since": None, "until": None, "hits": 1, "lines": made.searches[0]["lines"]}
    assert len(made.searches[0]["lines"]) == 1 and "worklog: Booked the Sydney hotel" in made.searches[0]["lines"][0]
    first, second = client.calls
    assert first["tools"] == ["search_history"] and second["tools"] == ["search_history"]  # rounds left: the tool stays
    assert "up to 5 searches this round" in first["messages"][0]["content"]
    assert "History searchable with search_history: 3 lines back to 2026-09-10." in first["messages"][1]["content"]  # the two worklog lines and the decision
    assert f"- [{new.id}] [" in first["messages"][1]["content"] and '"evidence"' in first["messages"][0]["content"]  # the worklog line carries its id; the shape asks for citations
    assert f"- [{declined.id}] [2026-10-01" in first["messages"][1]["content"]  # so does the decision from the other session
    assert second["messages"][2]["role"] == "assistant" and second["messages"][2]["tool_calls"][0]["function"]["name"] == "search_history"
    assert second["messages"][3]["role"] == "tool" and second["messages"][3]["tool_call_id"] == "call_0"
    assert f"[{old.id}] [2026-09-10" in second["messages"][3]["content"] and "worklog: Booked the Sydney hotel" in second["messages"][3]["content"]
    assert [row["kind"] for row in tail_jsonl(paths.model_calls, 10)] == ["proposer", "proposer"] and made.usage["total_tokens"] == 240
    assert tail_jsonl(paths.proposals, 1)[0]["searches"][0]["hits"] == 1
    # The cap: with one round, the second call keeps the tool defined but has the model answer (tool_choice none).
    client = FakeClient([[("search_history", {"query": "sydney", "since": "2026-09-01", "limit": "2"})], [("search_history", {"query": "paris"})]])
    proposer = Proposer(paths, client, fake_settings(proposer_search_rounds=1), stream=stream, session_id="s", index=index)
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-10-19T15:06:00Z", "app": "Safari", "window": "Qantas", "tree": "[0] AXWindow fares"})
    capped = await proposer.tick(now="2026-10-19T15:10:00Z")
    assert [(call["tools"], call["tool_choice"]) for call in client.calls] == [(["search_history"], "auto"), (["search_history"], "none")]
    assert "up to 1 search this round" in client.calls[0]["messages"][0]["content"]
    assert capped.searches[0] == {"query": "sydney", "regex": None, "kinds": ["worklog", "chat"], "since": "2026-09-01", "until": None, "hits": 1, "lines": capped.searches[0]["lines"]}  # the hotel; the proposal is not a default kind
    assert capped.action == "none" and "not a JSON object" in capped.summary  # it asked again instead of answering
    # The rounds at 0: no tool, no note on the prompt, an empty list on the record.
    client = FakeClient([proposal])
    proposer = Proposer(paths, client, fake_settings(proposer_search_rounds=0), stream=stream, session_id="s", index=index)
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-10-19T15:11:00Z", "app": "Safari", "window": "Qantas", "tree": "[0] AXWindow fares2"})
    plain = await proposer.tick(now="2026-10-19T15:15:00Z")
    assert client.calls[0]["tools"] == [] and client.calls[0]["tool_choice"] == "auto"
    assert "search_history" not in client.calls[0]["messages"][0]["content"] + client.calls[0]["messages"][1]["content"]
    assert plain.searches == [] and tail_jsonl(paths.proposals, 1)[0]["searches"] == []
    index.close()
