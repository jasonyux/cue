"""The idle trigger: its clock (proposer/idle.py), the observer's look on request, and a quick round in a session."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from live_helpers import FakeSource, make_manager, snap
from PIL import Image

from app.errors import RunnerCoreError
from app.live import session as session_module
from app.live.config import LiveSettings
from app.live.observer.recorder import record as record_row
from app.live.observer.recorder import run_observer
from app.live.proposer.idle import AWAY, FIRING, IDLE_MAX_S, OFF, SHOWN, IdleTrigger, ScreenWatch
from app.live.proposer.quick import stale
from app.live.storage.paths import SessionPaths, session_paths
from app.live.storage.records import Proposal, ProposalSet
from app.live.storage.stream import Stream
from app.live.utils.jsonl import append_jsonl, read_jsonl, tail_jsonl
from app.live.utils.time import seconds_between, timestamp


def armed(now=0.0, idle_s=10.0):
    trigger = IdleTrigger(cooldown_s=30, away_s=120)
    trigger.configure(True, idle_s, now)
    return trigger


def test_it_fires_once_per_pause_that_follows_work():
    trigger = armed()
    assert not trigger.step(5, 5, "a", True)    # no input since it was armed: a pause has to follow work
    assert not trigger.step(12, 2, "a", True)   # typing at 10
    assert trigger.step(21, 11, "a", True) and trigger.state == FIRING
    assert not trigger.step(22, 12, "a", True)  # one round at a time
    trigger.landed("p1")
    assert trigger.state == SHOWN
    assert not trigger.step(60, 50, "a", True)  # still idle: no second proposal
    assert not trigger.step(150, 140, "a", True) and trigger.state == AWAY  # no input two minutes after the fire


def test_a_second_fire_needs_input_on_a_changed_screen_after_the_cooldown():
    trigger = armed()
    assert trigger.step(21, 11, "a", True)
    trigger.landed(None)  # nothing shown
    assert not trigger.step(31, 1, "a", True) and trigger.state == SHOWN   # input, but the same screen
    assert not trigger.step(45, 1, "b", True) and trigger.state == SHOWN   # another screen, within the cooldown
    assert not trigger.step(52, 1, "b", True) and trigger.state == "armed"  # both, after it
    assert trigger.step(62, 11, "b", True)  # the next pause
    assert trigger.threshold == 10  # nothing was shown, so nothing was left unanswered


def test_an_unanswered_proposal_lengthens_the_next_pause_and_an_answer_resets_it():
    trigger = armed()
    now = 21.0
    assert trigger.step(now, 11, "a", True)
    for expected in (15.0, 22.5, 33.75, 50.625, IDLE_MAX_S, IDLE_MAX_S):
        trigger.landed(f"p{now}")
        now += 40
        assert not trigger.step(now, 1, f"screen{now}", True) and trigger.threshold == expected
        now += expected
        assert trigger.step(now, expected, f"screen{now - expected}", True)
    trigger.landed("answered")
    trigger.answered("answered")
    assert trigger.threshold == 10 and trigger.shown is None
    trigger.answered("another")  # someone else's proposal changes nothing
    assert trigger.threshold == 10


def test_it_holds_while_not_ready_and_off_never_fires():
    trigger = armed()
    assert not trigger.step(21, 11, "a", False)  # the console in front, a blocked app, a run going, the screen locked
    assert trigger.step(22, 12, "a", True)
    trigger.configure(False, 10, 30)
    assert trigger.state == OFF and not trigger.step(50, 40, "a", True)
    trigger.configure(True, 20, 100)  # on again: armed now, so the old input does not count
    assert trigger.threshold == 20 and not trigger.step(130, 40, "a", True)
    assert not trigger.step(131, 1, "a", True) and trigger.step(152, 21, "a", True)


def test_resumed_says_whether_the_person_went_on_after_the_fire():
    trigger = armed()
    assert not trigger.resumed(5, 1)  # never fired
    assert trigger.step(21, 11, "a", True)
    assert not trigger.resumed(25, 15) and trigger.resumed(25, 1)


def test_the_screen_watch_follows_the_rows_as_they_are_appended(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    watch = ScreenWatch(path)
    append_jsonl(path, snap("2026-10-15T10:00:00Z", "Mail", "Inbox", "AXWindow").wire())
    append_jsonl(path, {"ts": "2026-10-15T10:00:01Z", "kind": "screenshot_taken", "app": "Mail", "path": "x.png"})
    watch.update()
    first = watch.context
    assert watch.in_front()["app"] == "Mail"
    append_jsonl(path, {"ts": "2026-10-15T10:00:10Z", "kind": "screen_unchanged", "app": "Mail", "window": "Inbox"})
    watch.update()
    assert watch.context == first and watch.in_front()["ts"] == "2026-10-15T10:00:00Z"  # the same screen, read before
    append_jsonl(path, {**snap("2026-10-15T10:00:20Z", "1Password", "Vault", "").wire(), "excluded": True, "reason": "app"})
    watch.update()
    assert watch.context != first and watch.in_front() is None  # a blocked app is not read
    append_jsonl(path, snap("2026-10-15T10:00:30Z", "Terminal", "zsh", "AXTextArea value='error: x'").wire())
    watch.update()
    assert watch.in_front()["app"] == "Terminal"


def page(ts, readout, top=110, field=""):
    """A browser window: a page whose read-out line animates, scrolled to ``top``, with ``field`` typed in its search box."""
    lines = [f'AXLink "Section {n}" (40,{top + 24 * n} 900x20)' for n in range(12)]
    lines += [f'AXStaticText "approve {readout}" (40,{top + 300} 900x20)', f"AXSearchField \"Search\" value='{field}' (40,60 300x20)"]
    return snap(ts, "Google Chrome", "StartLux", "\n".join(f"[{index}] {line}" for index, line in enumerate(lines)))


def test_the_screen_key_ignores_content_that_moves_by_itself(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    watch = ScreenWatch(path)
    append_jsonl(path, page("2026-10-20T10:00:00Z", "0.10").wire())
    watch.update()
    first = watch.context
    for second, readout in ((10, "0.20"), (20, "0.30"), (30, "0.40")):  # the animation: a new hash each look, one line apart
        append_jsonl(path, page(f"2026-10-20T10:00:{second}Z", readout).wire())
        watch.update()
        assert watch.context == first
    append_jsonl(path, page("2026-10-20T10:00:40Z", "0.50", field="decision index").wire())  # typed into the page
    watch.update()
    typed = watch.context
    assert typed != first
    append_jsonl(path, page("2026-10-20T10:00:50Z", "0.60", top=-500, field="decision index").wire())  # scrolled: every line moved
    watch.update()
    assert watch.context not in (first, typed)


def test_a_mouse_that_only_moves_does_not_rearm_it():
    trigger = armed()
    assert trigger.step(21, 11, "a", True)
    trigger.landed(None)
    assert not trigger.step(60, 1, "b", True, acted=39) and trigger.state == SHOWN  # moved at 59, last pressed before the fire
    assert not trigger.step(61, 1, "b", True, acted=1) and trigger.state == "armed"  # a click at 60: armed for the next pause


def test_the_observer_looks_at_once_when_asked(tmp_path):
    paths = SessionPaths(tmp_path / "session").create()
    stream = Stream(tmp_path / "stream")
    source = FakeSource([snap("2026-10-15T10:00:00Z", "Mail", "Inbox", "a"), snap("2026-10-15T10:00:01Z", "Mail", "Inbox", "b")])
    clock = [0.0]
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        clock[0] += seconds
        paths.look_flag.touch()  # the idle trigger asks during the first nap

    ticks = run_observer(source, paths, stream=stream, interval=100, sleep=sleep, clock=lambda: clock[0], max_ticks=2)
    assert ticks == 2 and naps == [0.5] and not paths.look_flag.exists()  # one nap, not a hundred seconds of them


def test_a_look_the_trigger_asks_for_has_its_frame_even_on_an_unchanged_screen(tmp_path):
    paths = SessionPaths(tmp_path / "session").create()
    stream = Stream(tmp_path / "stream")
    same = snap("2026-10-21T10:00:00Z", "Code", "idle.py", "a")
    source = FakeSource([same, same.model_copy(update={"ts": "2026-10-21T10:00:10Z"}), same.model_copy(update={"ts": "2026-10-21T10:00:20Z"})])
    clock, naps = [0.0], []

    def sleep(seconds):
        naps.append(seconds)
        clock[0] += seconds
        if len(naps) == 2:
            paths.look_flag.touch()  # the trigger asks before the third look only

    run_observer(source, paths, stream=stream, interval=0.5, sleep=sleep, clock=lambda: clock[0], max_ticks=3)
    rows = read_jsonl(paths.snapshots)[0]
    assert [row["kind"] for row in rows] == ["screen_changed", "screenshot_taken", "screen_unchanged", "screen_unchanged", "screenshot_taken"]
    assert rows[-1]["ts"] == rows[-2]["ts"] and source.screenshots == 2  # the frame carries its look's time; an unasked look has none


def test_the_idle_settings_are_checked_and_placed_under_the_proposer(tmp_path):
    settings = LiveSettings.load()
    assert settings.proposer_idle_trigger is False and settings.proposer_idle_s == 10
    with pytest.raises(ValueError, match="proposerIdleS must be between 3 and 600"):
        settings.with_overrides({"proposerIdleS": 1})
    with pytest.raises(ValueError, match="proposerIdleProposals"):
        settings.with_overrides({"proposerIdleProposals": 9})
    settings.save({"proposerIdleTrigger": True, "proposerIdleS": 15})
    again = LiveSettings.load()
    assert again.proposer_idle_trigger is True and again.proposer_idle_s == 15
    assert again.wire()["proposerIdleTrigger"] is True and "idle_trigger: true" in (tmp_path / "settings.yaml").read_text()


def test_a_stale_round_keeps_its_proposals_out_of_view():
    proposals = ProposalSet(action="propose", task="Explain it", proposals=[Proposal(kind="explain", task="Explain it")], trigger="idle")
    late = stale(proposals)
    assert late.effect == "stale" and late.action == "none" and late.task is None and late.proposals == []
    assert [(item.task, item.source) for item in late.filtered] == [("Explain it", "stale")]


async def test_an_idle_round_proposes_on_a_fresh_look_and_its_answer_resets_the_pause(tmp_path, monkeypatch):
    reply = '{"summary": "A failing test.", "proposals": [{"kind": "explain", "task": "Explain the KeyError in test_api", "reason": "red output", "confidence": 4}]}'
    manager, client, _ = make_manager(tmp_path / "data", [reply], notify=False, proposer_interval_s=3600, summary_interval_s=3600,
                                      belief_interval_s=3600, proposer_idle_trigger=True, proposer_idle_s=0.2)
    last_input = [time.monotonic() + 3600]  # set below, once the session is armed
    monkeypatch.setattr(session_module, "IDLE_POLL_S", 0.02)
    monkeypatch.setattr(session_module, "input_idle_s", lambda: max(0.0, time.monotonic() - last_input[0]))
    monkeypatch.setattr(session_module, "action_idle_s", lambda: max(0.0, time.monotonic() - last_input[0]))
    monkeypatch.setattr(session_module, "screen_locked", lambda: False)
    monkeypatch.setattr(manager, "_idle_beliefs", AsyncMock())  # the belief refresh beside it has its own test
    session_id = (await manager.start())["session"]["id"]
    paths = session_paths(tmp_path / "data", session_id)
    record_row(paths, manager.memory.stream, {**snap(timestamp(), "Terminal", "pytest", "AXTextArea value='KeyError: x'").wire()})

    async def observer():  # the recorder's part: a look when asked, then that look's frame
        while not paths.look_flag.exists():
            await asyncio.sleep(0.01)
        paths.look_flag.unlink()
        looked = timestamp()
        record_row(paths, manager.memory.stream, {"ts": looked, "kind": "screen_unchanged", "app": "Terminal", "window": "pytest"})
        await asyncio.sleep(0.05)
        Image.new("RGB", (8, 6)).save(paths.screenshots / "frame.png")
        record_row(paths, manager.memory.stream, {"ts": looked, "kind": "screenshot_taken", "app": "Terminal", "window": "pytest", "path": "frame.png"})

    looking = asyncio.create_task(observer())
    last_input[0] = time.monotonic()  # the person types, then stops
    await asyncio.wait_for(looking, 5)
    for _ in range(200):
        if manager.session.idle.state == SHOWN:
            break
        await asyncio.sleep(0.02)
    made = tail_jsonl(paths.proposals, 1)[0]
    assert made["trigger"] == "idle" and made["effect"] == "proposed" and made["task"] == "Explain the KeyError in test_api"
    assert manager.session.idle.shown == made["ts"] and manager._idle_beliefs.await_count == 1
    calls = read_jsonl(paths.model_calls)[0]
    assert [call["kind"] for call in calls] == ["proposer_idle"]
    system, user = client.calls[0]["messages"]
    assert "They have just paused" in system["content"] and client.calls[0]["thinking"] is True
    assert "The person paused on Terminal — \"pytest\"" in user["content"] and "KeyError: x" in user["content"]
    assert client.calls[0]["images"] == 1  # the frame of the look it asked for, waited for after the look
    assert "<observations> end when the pause began" in user["content"]
    status = manager.status()["session"]["idleTrigger"]
    assert status["enabled"] is True and status["state"] == SHOWN and status["firedAt"]

    await manager.record_feedback(made["ts"], "accept")  # its digest finds the idle round's call
    await manager.wait_for_digests()
    assert "digestError" not in tail_jsonl(paths.feedback, 1)[0] and manager.session.idle.shown is None
    await manager.stop()


async def test_the_menu_switch_changes_the_running_session_and_the_default(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data", proposer_interval_s=3600, summary_interval_s=3600)
    await manager.start({"notify": False})
    assert manager.status()["session"]["idleTrigger"]["state"] == OFF
    status = manager.set_idle_trigger(True, 12)
    assert status["session"]["idleTrigger"]["enabled"] is True and status["session"]["idleTrigger"]["idleS"] == 12
    assert manager.session.settings.proposer_idle_trigger is True and manager.defaults.proposer_idle_s == 12
    assert status["defaults"]["proposerIdleTrigger"] is True
    with pytest.raises(RunnerCoreError, match="proposerIdleS"):
        manager.set_idle_trigger(True, 1)
    assert manager.set_idle_trigger(False, 12)["session"]["idleTrigger"]["state"] == OFF
    await manager.stop()


async def test_the_sliders_tune_the_running_session_and_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "input_idle_s", lambda: None)  # no Quartz: the status skips the input clock
    manager, _client, _ = make_manager(tmp_path / "data", proposer_interval_s=3600, summary_interval_s=3600)
    await manager.start({"notify": False})
    session = manager.session
    status = manager.tune({"proposerIntervalS": 90, "proposerIdleCooldownS": 15, "proposerDedupThreshold": 0.65})
    assert status["session"]["settings"]["proposerIntervalS"] == 90 and status["defaults"]["proposerDedupThreshold"] == 0.65
    assert session.proposer.settings is session.settings and session.quick.settings.proposer_dedup_threshold == 0.65
    assert session.idle.cooldown_s == 15 and manager.defaults.proposer_idle_cooldown_s == 15
    await asyncio.sleep(0)  # the proposer's loop started over
    assert 0 < seconds_between(timestamp(), session.next_proposal_at) <= 90
    with pytest.raises(RunnerCoreError, match="proposerModel"):
        manager.tune({"proposerModel": "other"})
    with pytest.raises(RunnerCoreError, match="at least 15"):
        manager.tune({"proposerIntervalS": 1})
    assert session.settings.proposer_interval_s == 90 and manager.defaults.proposer_interval_s == 90
    await manager.stop()


def test_a_round_the_person_went_on_before_is_kept_but_not_shown(tmp_path, monkeypatch):
    manager, _client, _ = make_manager(tmp_path / "data", notify=False)
    session = type("Held", (), {})()
    session.idle = armed()
    session.settings = manager.defaults
    session.idle.step(21, 11, "a", True)
    monkeypatch.setattr(session_module, "monotonic", lambda: 25.0)
    record = ProposalSet(action="propose", task="Do it", proposals=[Proposal(task="Do it")])
    monkeypatch.setattr(session_module, "action_idle_s", lambda: 1.0)  # a key or a click at 24, after the fire at 21
    assert manager._idle_finalize(session, record).effect == "stale"
    monkeypatch.setattr(session_module, "input_idle_s", lambda: 1.0)  # the mouse moved at 24, but nothing was pressed
    monkeypatch.setattr(session_module, "action_idle_s", lambda: 10.0)
    assert manager._idle_finalize(session, record).effect == "proposed"


def test_the_idle_prompt_takes_a_long_look_as_a_cue():
    from app.live.proposer.prompts import quick_system
    text = quick_system(1, ["current_tree", "digest"])
    assert "plain reading" not in text  # a person staring at a page was told off as "plain reading"; a long look is a cue
    assert "A long look at\nthe same content" in text and "Content that moves on its own" in text
    # a selection marks where they look, read in its surroundings and their work; no relation the screen does not state
    assert "it is not all they need" in text and "never state a relation you inferred" in text


def test_the_selection_reaches_the_digest_the_screen_key_and_the_pause_note(tmp_path):
    from app.live.observer.digest import build_window_digest
    from app.live.proposer.quick import pause_note
    from app.live.storage.records import Snapshot

    first = page("2026-10-20T10:00:00Z", "0.10")
    marked = page("2026-10-20T10:00:10Z", "0.10").model_copy(update={"selection": "Decision Index 0.3"})
    path = tmp_path / "snapshots.jsonl"
    watch = ScreenWatch(path)
    append_jsonl(path, first.wire())
    watch.update()
    before = watch.context
    append_jsonl(path, marked.wire())
    watch.update()
    assert watch.context != before  # selecting text is a new screen, though no line changed
    assert 'selected: "Decision Index 0.3"' in build_window_digest([first, marked])
    note = pause_note(12, Snapshot.model_validate(watch.in_front()))
    assert note.endswith('Selected text: "Decision Index 0.3"') and "for the last 12 seconds" in note


def test_the_window_is_excerpted_around_the_selection():
    from app.live.observer.digest import NEAR_LINES, excerpt_tree

    header = 'Chrome · window "page"'
    lines = [f'[{index}] AXStaticText "line {index}" (0,{index * 20} 400x20)' for index in range(200)]
    tree = "\n".join([header, *lines])
    near = lines[150 - NEAR_LINES: 150 + NEAR_LINES + 1]
    budget = len(header) + 1 + sum(len(line) + 1 for line in near)  # room for the lines around the selection only
    assert excerpt_tree(tree, None, budget, near="LINE 150").splitlines()[1:] == [*near, f"… (+{200 - len(near)} more lines)"]
    assert excerpt_tree(tree, None, budget).splitlines()[1] == lines[0]  # without one: the top of the tree, as before

