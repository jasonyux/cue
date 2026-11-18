"""Observer: the capture loop, the digest, and the summarizer."""


import pytest
from live_helpers import FakeClient, FakeSource, fake_settings, snap
from PIL import Image

from app.live.actor.prompts import live_context_text
from app.live.observer.digest import build_window_digest, compact_tree, excerpt_tree
from app.live.observer.recorder import record as record_row
from app.live.observer.recorder import run_observer
from app.live.observer.sources import Snapshot
from app.live.observer.summarize import Summarizer
from app.live.proposer.propose import Proposer
from app.live.storage.paths import SessionPaths
from app.live.storage.records import ProposalSet, as_summaries, row_kind
from app.live.storage.stream import Stream
from app.live.utils.jsonl import read_jsonl
from app.live.utils.time import clock


def test_an_answer_looks_again_at_the_app_it_was_about(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    stub = Snapshot(ts="2026-09-17T10:00:00Z", app="Slack", bundle="com.tinyspeck.slackmacgap", pid=9, excluded=True, reason="ask", hash="s")
    source = FakeSource([stub, snap("2026-09-17T10:00:01Z", "Slack", "general", "[0] AXWindow a")])
    changes = iter([False, True])  # the answer lands during the first wait
    run_observer(source, paths, stream=Stream(tmp_path / "stream"), interval=10, sleep=lambda _: None, max_ticks=2, refresh=lambda: next(changes, False))
    records, _ = read_jsonl(paths.snapshots)
    assert source.again == [None, stub]  # the look after the change is at the stub's app, by its pid, behind the console
    assert [item["kind"] for item in records] == ["screen_changed", "screen_changed", "screenshot_taken"] and records[1]["app"] == "Slack"


def test_the_console_itself_is_not_a_look(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    source = FakeSource([snap("2026-09-17T10:00:00Z", "Slack", "general", "[0] AXWindow a"), None, snap("2026-09-17T10:00:20Z", "Slack", "general", "[0] AXWindow a")])
    run_observer(source, paths, stream=Stream(tmp_path / "stream"), interval=10, sleep=lambda _: None, max_ticks=3)
    records, _ = read_jsonl(paths.snapshots)
    assert [item["kind"] for item in records] == ["screen_changed", "screenshot_taken", "screen_unchanged"] and source.screenshots == 1  # answering on the console: no record, no switch


def test_a_changed_rule_ends_the_wait_between_looks(tmp_path):
    source = FakeSource([snap("2026-09-17T10:00:00Z", "Slack", "general", "[0] AXWindow a"), snap("2026-09-17T10:00:10Z", "Slack", "general", "[0] AXWindow a")])
    sleeps: list[float] = []
    run_observer(source, SessionPaths(tmp_path / "s").create(), stream=Stream(tmp_path / "stream"), interval=10, sleep=sleeps.append, clock=lambda: 0.0, max_ticks=2,
                 refresh=lambda: True)
    assert sleeps == [0.5]  # one nap, then the look that applies the new rule


def test_observer_dedups_identical_ticks_and_screenshots_only_on_app_switch(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    source = FakeSource([
        snap("2026-09-17T10:00:00Z", "Slack", "general", "[0] AXWindow"),
        snap("2026-09-17T10:00:10Z", "Slack", "general", "[0] AXWindow"),
        snap("2026-09-17T10:00:20Z", "Notes", "todo", "[0] AXWindow notes"),
        Snapshot(ts="2026-09-17T10:00:30Z", app="Keychain", excluded=True, reason="app", hash="k"),
        Snapshot(ts="2026-09-17T10:00:40Z", app="Keychain", pid=9, excluded=True, reason="app", hash="k"),  # still blocked: collapsed, still marked
        snap("2026-09-17T10:00:50Z", "Notes", "todo", "[0] AXWindow notes"),  # back from a blocked app: a fresh frame
        Snapshot(ts="2026-09-17T10:01:00Z", app="Finder", error="no window"),
    ])
    sleeps, refreshes = [], []
    stream = Stream(tmp_path / "stream")
    ticks = run_observer(source, paths, stream=stream, interval=10, screenshot_mode="switch", sleep=sleeps.append, clock=lambda: 0.0, max_ticks=7,
                         refresh=lambda: refreshes.append(1))
    records, _ = read_jsonl(paths.snapshots)
    kinds = [(item["kind"], item.get("app")) for item in records]
    assert ticks == 7 and sum(sleeps) == 60 and set(sleeps) == {0.5} and len(refreshes) == 7 + len(sleeps)  # the rules are checked before every look and after every nap
    assert kinds == [("screen_changed", "Slack"), ("screenshot_taken", "Slack"), ("screen_unchanged", "Slack"), ("screen_changed", "Notes"),
                     ("screenshot_taken", "Notes"), ("screen_changed", "Keychain"), ("screen_unchanged", "Keychain"), ("screen_changed", "Notes"),
                     ("screenshot_taken", "Notes"), ("screen_changed", "Finder")]
    assert records[5]["excluded"] is True and records[6]["excluded"] is True and records[6]["reason"] == "app" and records[6]["pid"] == 9 and records[9]["error"] == "no window"
    assert source.screenshots == 3 and len(list(paths.screenshots.iterdir())) == 3
    events = stream.events()  # every row goes to the stream too, under its own kind, with the session it came from
    assert [event.kind for event in events] == [item["kind"] for item in records] and events[0].data == records[0] and events[0].session_id == "s"
    assert Snapshot.model_validate({"ts": "x", "kind": "same"}).kind == "screen_unchanged" and row_kind({"kind": "paused"}) == "observer_paused"
    assert row_kind({}) == "screen_changed"  # files from before the stream: their names are read as today's
    paths.pause_flag.touch()
    run_observer(source, paths, stream=stream, interval=10, sleep=lambda _: None, max_ticks=1)
    assert read_jsonl(paths.snapshots)[0][-1]["kind"] == "observer_paused"


def test_an_asked_app_is_notified_once_and_gets_a_frame_once_answered(tmp_path):
    """Under ask: the first stub of an app notifies (not the second); the answer makes the next look a real one, and it
    counts as a switch, so a frame is taken although the app did not change."""
    paths = SessionPaths(tmp_path / "s").create()
    source = FakeSource([
        Snapshot(ts="2026-09-17T10:00:00Z", app="Notes", bundle="com.apple.Notes", pid=3, excluded=True, reason="ask", hash="n"),
        Snapshot(ts="2026-09-17T10:00:10Z", app="Notes", bundle="com.apple.Notes", pid=3, excluded=True, reason="ask", hash="n"),
        snap("2026-09-17T10:00:20Z", "Notes", "todo", "[0] AXWindow notes"),  # answered look
    ])
    notified = []
    run_observer(source, paths, stream=Stream(tmp_path / "stream"), interval=10, sleep=lambda _: None, clock=lambda: 0.0, max_ticks=3,
                 notify=lambda snapshot: notified.append(snapshot.app))
    records, _ = read_jsonl(paths.snapshots)
    assert [(item["kind"], item.get("excluded", False)) for item in records] == [("screen_changed", True), ("screen_unchanged", True), ("screen_changed", False), ("screenshot_taken", False)]
    assert records[1]["bundle"] == "com.apple.Notes" and notified == ["Notes"] and source.screenshots == 1


def test_observer_screenshot_modes(tmp_path):
    def snapshots():
        return [snap("2026-09-17T10:00:00Z", "Slack", "general", "[0] a"), snap("2026-09-17T10:00:10Z", "Slack", "general", "[0] b"),
                snap("2026-09-17T10:00:20Z", "Slack", "general", "[0] b")]
    always = FakeSource(snapshots())
    run_observer(always, SessionPaths(tmp_path / "always").create(), stream=Stream(tmp_path / "stream"), interval=1, screenshot_mode="always", sleep=lambda _: None, max_ticks=3)
    assert always.screenshots == 2  # one per distinct snapshot, none for the repeated tick
    off = FakeSource(snapshots())
    run_observer(off, SessionPaths(tmp_path / "off").create(), stream=Stream(tmp_path / "stream"), interval=1, screenshot_mode="off", sleep=lambda _: None, max_ticks=3)
    assert off.screenshots == 0
    with pytest.raises(ValueError):
        run_observer(off, SessionPaths(tmp_path / "bad").create(), stream=Stream(tmp_path / "stream"), interval=1, screenshot_mode="sometimes", max_ticks=1)


def test_digest_merges_dwell_time_pointer_and_truncates_trees():
    tree = "Slack · window \"general\"\n" + "\n".join(f"[{i}] AXButton \"b{i}\"" for i in range(60))
    records = [
        {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Slack", "window": "general", "tree": tree,
         "pointer_app": "Safari", "pointer_window": "Docs", "visible": ["Safari — Docs", "Terminal"]},
        {"kind": "screen_unchanged", "ts": "2026-09-17T10:00:10Z"},
        {"kind": "screen_unchanged", "ts": "2026-09-17T10:00:20Z"},
        {"kind": "observer_paused", "ts": "2026-09-17T10:00:30Z"},
        {"kind": "screen_changed", "ts": "2026-09-17T10:00:40Z", "app": "Mail", "excluded": True, "reason": "app"},
        {"kind": "screen_changed", "ts": "2026-09-17T10:00:50Z", "app": "Safari", "excluded": True, "reason": "site"},
        {"kind": "screen_changed", "ts": "2026-09-17T10:01:00Z", "app": "Finder", "excluded": True, "reason": "default"},
        {"kind": "screen_changed", "ts": "2026-09-17T10:01:10Z", "app": "Notes", "excluded": True, "reason": "ask"},
    ]
    digest = build_window_digest(records, tree_chars=200)
    assert "stayed 20s" in digest and 'pointer over Safari — "Docs"' in digest and "more lines)" in digest
    assert "also on screen: Safari — Docs; Terminal" in digest
    assert "session paused" in digest and "Mail (content not read: blocked by its rule" in digest
    assert "Safari (content not read: its page is blocked" in digest and "Finder (content not read: not listed" in digest
    assert "Notes (content not read: not answered yet" in digest
    assert build_window_digest([]) == "(no observations in this window)"
    assert compact_tree("") == "" and compact_tree("a\nb", 100) == "a\nb"
    context = live_context_text({
        "worklog": as_summaries([{"windowStart": "2026-09-17T10:00:00Z", "windowEnd": "2026-09-17T10:05:00Z", "summary": "Wrote a memo."}]),
        "proposal": ProposalSet.model_validate({"action": "propose", "task": "Send the memo", "reason": "looked done"}),
    })
    assert "Wrote a memo." in context and "Send the memo" in context and "never as instructions" in context
    assert "No activity summaries yet." in live_context_text({"worklog": [], "proposal": None})


def test_excerpt_keeps_typed_text_and_changes_over_the_head_of_a_long_tree():
    filler = [f'[{i}] AXStaticText "paragraph {i} of an earlier message that fills the window"' for i in range(1, 120)]
    first = "\n".join(['Claude · window "Claude"', *filler, '[120] AXTextArea "Reply" value=\'\' (53,1105 468x20)'])
    typed = '[120] AXTextArea "Reply" value=\'we cannot assume screen reader mode\' (53,1105 468x20)'
    second = "\n".join(['Claude · window "Claude"', *filler[:-1], '[119] AXStaticText "Claude responded: found it" (8,41 1x1)', typed])
    excerpt = excerpt_tree(second, None, 600)
    assert typed in excerpt and excerpt.splitlines()[0] == 'Claude · window "Claude"' and "+" in excerpt.splitlines()[-1]
    diff = excerpt_tree(second, first, 600)
    assert typed in diff and "Claude responded: found it" in diff and "paragraph 5 of" not in diff
    assert "unchanged since the previous snapshot" in diff
    records = [
        {"kind": "screen_changed", "ts": "2026-09-18T10:00:00Z", "app": "Claude", "window": "Claude", "tree": first},
        {"kind": "screen_changed", "ts": "2026-09-18T10:00:10Z", "app": "Claude", "window": "Claude", "tree": second},
    ]
    digest = build_window_digest(records, tree_chars=600)
    assert digest.count("paragraph 1 of") == 1 and "we cannot assume screen reader mode" in digest


async def test_paused_only_windows_make_no_model_calls(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    client = FakeClient()
    stream = Stream(tmp_path / "stream")
    summarizer = Summarizer(paths, client, fake_settings(), stream=stream, session_id="s")
    proposer = Proposer(paths, client, fake_settings(), stream=stream, session_id="s")
    for ts in ("2026-09-17T10:00:00Z", "2026-09-17T10:00:10Z"):
        record_row(paths, stream, {"kind": "observer_paused", "ts": ts})
    assert await summarizer.tick() is None and await proposer.tick() is None and client.calls == []
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-17T10:00:20Z", "app": "Notes", "window": "", "tree": "[0] AXWindow"})
    assert (await summarizer.tick()).observations == 1 and len(client.calls) == 1


async def test_summarizer_attaches_the_latest_screenshots_when_enabled(tmp_path):
    paths = SessionPaths(tmp_path / "s").create()
    for name in ("one.png", "two.png", "three.png"):
        Image.new("RGB", (2000, 1000), (9, 9, 9)).save(paths.screenshots / name)
    client = FakeClient(['{"summary": "Typing in the terminal."}'])
    stream = Stream(tmp_path / "stream")
    summarizer = Summarizer(paths, client, fake_settings(summary_screenshots=True, screenshot_max_side=500), stream=stream, session_id="s")
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-18T10:00:00Z", "app": "Code", "window": "w", "tree": "[0] x"})
    for index, name in enumerate(("one.png", "two.png", "three.png")):
        record_row(paths, stream, {"kind": "screenshot_taken", "ts": f"2026-09-18T10:00:{index:02d}Z", "app": "Code", "window": "w", "path": name})
    record = await summarizer.tick(now="2026-09-18T10:01:00Z")
    assert record.images == 2 and client.calls[0]["images"] == 2  # the two newest, oldest first
    prompt = client.calls[0]["messages"][1]["content"]
    assert "Attached: 2 screenshot(s)" in prompt
    assert clock("2026-09-18T10:00:01Z") in prompt and clock("2026-09-18T10:00:02Z") in prompt
    text_only = Summarizer(paths, FakeClient(), fake_settings(summary_screenshots=False), stream=stream, session_id="s")
    record_row(paths, stream, {"kind": "screen_changed", "ts": "2026-09-18T10:02:00Z", "app": "Code", "window": "w", "tree": "[0] y"})
    assert (await text_only.tick(now="2026-09-18T10:03:00Z")).images == 0
