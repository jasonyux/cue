"""Offline replay: a stage runs again over the windows its model calls recorded, read from the stream, into a separate folder."""

from live_helpers import FakeClient, fake_settings

from app.live.replay import replay, side_by_side
from app.live.storage.paths import SessionPaths
from app.live.storage.records import Summary
from app.live.storage.stream import Stream
from app.live.utils.jsonl import append_jsonl, read_jsonl, write_json


class LoudSummaryStrategy:
    """A stand-in strategy: shouts the number of observations instead of asking the model anything useful."""

    def messages(self, inputs):
        return [{"role": "user", "content": f"{len(inputs.input['snapshots'])} observations, {len(inputs.input['worklog'])} previous"}]

    def parse(self, reply, inputs):
        return Summary(summary=f"LOUD {reply.content}")


def call(ts, kind, start, end):
    return {"id": "c", "ts": ts, "kind": kind, "windowStart": start, "windowEnd": end}


async def test_replay_reruns_each_recorded_window_from_the_stream(tmp_path):
    session = SessionPaths(tmp_path / "sessions" / "s1").create()
    write_json(session.session_json, {"id": "s1", "settings": {"summaryModel": "old-model", "maxProposals": 2}})
    stream = Stream(tmp_path / "stream")
    for index in range(4):
        ts = f"2026-09-21T10:0{index}:00Z"
        stream.emit("screen_changed", {"kind": "screen_changed", "ts": ts, "app": "Code", "window": "w", "tree": f"[{index}] x"}, ts=ts, session_id="s1")
    for ts, start, text in (("2026-09-21T10:01:30Z", "2026-09-21T10:00:00Z", "first window"),
                            ("2026-09-21T10:03:30Z", "2026-09-21T10:02:00Z", "second window")):
        row = {"ts": ts, "windowStart": start, "windowEnd": ts, "summary": text}
        stream.emit("summary_written", row, ts=ts, session_id="s1")
        append_jsonl(session.summaries, row)
        append_jsonl(session.model_calls, call(ts, "summary", start, ts))
    append_jsonl(session.model_calls, {"id": "x", "ts": "2026-09-21T10:04:00Z", "kind": "summary_truncated"})  # no window: not replayed
    stream.emit("run_started", {"ts": "2026-09-21T10:02:30Z", "runId": "r1", "event": "started", "prompt": "Fix the build"},
                ts="2026-09-21T10:02:30Z", session_id="s1")
    stream.emit("proposal_rejected", {"ts": "2026-09-21T09:59:00Z", "proposalsTs": "x", "decision": "reject", "kind": "act", "task": "Old idea",
                             "context": "reading mail"}, ts="2026-09-21T09:59:00Z", session_id="s1")
    append_jsonl(session.proposals, {"ts": "2026-09-21T10:03:40Z", "windowStart": "2026-09-21T10:00:00Z", "windowEnd": "2026-09-21T10:03:40Z",
                                     "action": "none", "proposals": []})
    append_jsonl(session.model_calls, call("2026-09-21T10:03:40Z", "proposer", "2026-09-21T10:00:00Z", "2026-09-21T10:03:40Z"))
    client = FakeClient(["a", "b"])
    out = tmp_path / "out"
    replayed = await replay(session, "summary", stream=stream, settings=fake_settings(), client=client, out=out, strategy=LoudSummaryStrategy())
    assert [item.summary for item in replayed] == ["LOUD a", "LOUD b"]
    assert client.calls[0]["messages"][0]["content"] == "2 observations, 0 previous"
    assert client.calls[1]["messages"][0]["content"] == "2 observations, 1 previous"  # the stored first line, as the live call saw it
    written, _ = read_jsonl(out / "summaries.jsonl")
    assert [item["summary"] for item in written] == ["LOUD a", "LOUD b"] and (out / "replay.json").is_file()
    assert read_jsonl(session.summaries)[0][0]["summary"] == "first window" and stream.events()[-1].kind == "proposal_rejected"  # untouched
    assert (out / "screenshots").is_symlink() and [event.kind for event in Stream(out / "stream").events()] == ["summary_written", "summary_written"]
    report = side_by_side(read_jsonl(session.summaries)[0], replayed, "summary")
    assert "stored:   first window" in report and "replayed: LOUD a" in report

    proposer_client = FakeClient(['{"summary": "s", "proposals": [{"kind": "explain", "task": "Explain it"}]}'])
    proposed = await replay(session, "proposer", stream=stream, settings=fake_settings(), client=proposer_client, out=tmp_path / "proposer")
    assert proposed[0].proposals[0].task == "Explain it" and proposed[0].observations == 4
    prompt = proposer_client.calls[0]["messages"][1]["content"]
    assert "first window" in prompt and "second window" in prompt  # the session's worklog before the window's end
    assert "- Fix the build" in prompt  # the run that was in progress at the window's end
    assert "rejected (act): Old idea · while: reading mail" in prompt  # the decision before the window
    assert "explain" in side_by_side(read_jsonl(session.proposals)[0], proposed, "proposer")
