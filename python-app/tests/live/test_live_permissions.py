"""Permissions: the level of each app and site, and the observer keeping only a stub of what they hide."""

import time
from types import SimpleNamespace

import pytest

from app.live import permissions
from app.live.observer import sources
from app.live.observer.sources import Snapshot
from app.live.permissions import (
    ASK,
    Level,
    Permissions,
    Reason,
    Rules,
    address,
    answer_ask,
    default_of,
    pending_asks,
    rules,
    session_rules,
    write_session_rules,
)
from app.live.utils.jsonl import read_jsonl


def test_strictest_matching_rule_wins_and_the_default_covers_the_rest():
    given = Permissions(Level.LOOK, rules("Slack: look, com.apple.MobileSMS: block, 1Password*: block, Google Chrome: use", "apps"),
                        rules("chase.com: block, *bank*: block, reddit.com/r/private*: block", "sites"))
    assert given.level("Slack") == "look" and given.level("Messages", "com.apple.mobilesms") == "block"  # by name or bundle id, any case
    assert given.level("1Password 8") == "block" and given.level("Finder") == "look"  # a glob; the default
    assert given.level("Google Chrome", address="github.com/jasonyux") == "use"
    assert given.level("Google Chrome", address="online.chase.com/login") == "block"  # a bare domain covers its subdomains
    assert given.level("Google Chrome", address="mychase.com") == "use"  # not a subdomain
    assert given.level("Google Chrome", address="www.bankofamerica.com/") == "block"  # a glob on the address
    assert given.level("Google Chrome", address="www.reddit.com/r/private/comments/1") == "block"  # a path, under a subdomain
    assert given.level("Safari", address="online.chase.com") == "block"  # site rules apply in any browser
    assert Permissions(Level.BLOCK).decide("Finder") == (Level.BLOCK, Reason.DEFAULT)
    # the reason is the rule that set the level: the app rule on a tie, since the page is never read when the app is none
    assert (given.decide("Slack"), given.decide("Finder")) == ((Level.LOOK, Reason.APP), (Level.LOOK, Reason.DEFAULT))
    assert given.decide("Google Chrome", address="online.chase.com") == (Level.BLOCK, Reason.SITE)
    assert given.decide("Messages", "com.apple.MobileSMS", address="online.chase.com") == (Level.BLOCK, Reason.APP)
    assert given.decide("Slack", address="online.chase.com") == (Level.BLOCK, Reason.SITE)  # Slack: look, the site rule is stricter


def test_rules_from_the_pages_text_and_the_files_mapping():
    assert rules("Slack: look, Code: USE, 1Password*: block,", "permissionsApps") == {"Slack": Level.LOOK, "Code": Level.USE, "1Password*": Level.BLOCK}
    assert rules({"chase.com": "block"}, "permissionsSites") == {"chase.com": "block"} and rules("", "permissionsApps") == {}
    for bad in ("Slack", "Slack: read", ": look", ["Slack"], {"": "look"}):
        with pytest.raises(ValueError):
            rules(bad, "permissionsApps")


def test_address_is_the_browsers_bar_and_nothing_else():
    def field(role, value):
        return SimpleNamespace(role=role, value=value)
    tree = [field("AXButton", "Reload"), field("AXTextField", "https://github.com/jasonyux/computerpilot"), field("AXTextField", "chase.com")]
    assert address(tree) == "github.com/jasonyux/computerpilot"
    assert address([field("AXTextField", "github.com/x")]) == "github.com/x"  # as Chrome shows it, without the scheme
    assert address([field("AXTextField", "how to cook rice"), field("AXTextField", "john@mail.com"), field("AXStaticText", "https://a.com")]) is None


def test_observer_source_keeps_only_a_stub_of_what_its_permissions_hide(monkeypatch):
    source = object.__new__(sources.AccessibilitySource)
    source.max_elements = 400
    source.permissions = Permissions(Level.LOOK, {"Messages": Level.BLOCK}, {"chase.com": Level.BLOCK})
    apps = iter([("Messages", "com.apple.MobileSMS"), ("Slack", "com.tinyspeck.slackmacgap"), ("Safari", "com.apple.Safari"), ("Safari", "com.apple.Safari"),
                 ("Finder", "com.apple.finder"), ("Finder", "com.apple.finder")])
    pages = iter(["https://online.chase.com/login", "https://online.chase.com/login", "https://docs.python.org/3/"])

    def frontmost_app():
        name, bundle = next(apps)
        return SimpleNamespace(localizedName=lambda: name, bundleIdentifier=lambda: bundle, processIdentifier=lambda: 7)

    def get_state(pid, screenshot, max_elements):
        return SimpleNamespace(window_title="page", text="[0] AXWindow", elements=[SimpleNamespace(role="AXTextField", value=next(pages, ""))],
                               screenshot="the window's own pixels" if screenshot else None)

    monkeypatch.setattr("app.desktop.ax.client.frontmost_app", frontmost_app)
    monkeypatch.setattr("app.desktop.ax.client.pump_run_loop", lambda: None)
    monkeypatch.setattr(sources, "onscreen_windows", list)
    monkeypatch.setattr(sources, "window_under_pointer", lambda windows: None)
    source.desktop = SimpleNamespace(get_state=get_state)
    hidden, slack, bank, docs, finder = (source.snapshot() for _ in range(5))
    assert slack.excluded is False and slack.tree == "[0] AXWindow"  # the same URL in a message box: content, not an address
    assert (hidden.app, hidden.bundle, hidden.excluded, hidden.reason, hidden.tree) == ("Messages", "com.apple.MobileSMS", True, "app", "")
    assert hidden.hash and hidden.hash != bank.hash  # a blocked look hashes its app and reason, so staying there collapses into unchanged ticks
    assert (bank.app, bank.excluded, bank.reason, bank.window, bank.tree) == ("Safari", True, "site", "", "")  # the page was read, nothing of it kept
    assert docs.excluded is False and docs.tree == "[0] AXWindow" and finder.excluded is False
    source.permissions = Permissions(Level.BLOCK)  # the rules replaced mid-session: the next look follows them
    assert (source.snapshot().reason, source.allowed("Finder", "")) == ("default", False)
    apps = iter([("ComputerPilot Console", "com.vivaldi.Vivaldi.app.hbblfifohofgngfbjbiimbbcimepbdcb"), ("Vivaldi", "com.vivaldi.Vivaldi")])
    monkeypatch.setattr(sources, "onscreen_windows", lambda: [{"app": "Vivaldi", "title": "Cue Console - Vivaldi", "pid": 7, "bounds": (0, 0, 1, 1)}])
    assert source.snapshot() is None and source.snapshot() is None  # the console itself, installed (under the name it had then) or as a tab: not a look, whatever the rules
    monkeypatch.setattr(sources, "onscreen_windows", list)
    source.permissions, source.desktop.visible_snapshot = Permissions(Level.LOOK), lambda: "a display cut"
    again = source.snapshot(Snapshot(ts="x", app="Finder", bundle="com.apple.finder", pid=7, excluded=True, reason="ask", hash="f"))  # just allowed: that app, by pid
    assert (again.app, again.pid, again.excluded, again.tree) == ("Finder", 7, False, "[0] AXWindow")
    assert source.screenshot() == "the window's own pixels" and source.screenshot() == "a display cut"  # behind the console: its own pixels, once


def test_ask_is_a_default_and_never_a_rule(tmp_path):
    """Under ask, an app no rule names is blocked with the reason ask (asked about, until answered); a rule matching it decides as usual."""
    asking = Permissions(ASK, rules("Slack: look", "apps"))
    assert asking.decide("Finder") == (Level.BLOCK, Reason.ASK) and asking.decide("Slack") == (Level.LOOK, Reason.APP)
    assert default_of(" Ask ", "permissionsDefault") == ASK and default_of("USE", "permissionsDefault") == Level.USE
    with pytest.raises(ValueError, match="must be one of ask, block, look, use"):
        default_of("none", "permissionsDefault")
    with pytest.raises(ValueError, match="must be one of block, look, use"):
        rules("Slack: ask", "permissionsApps")  # a rule holds a level
    # A session's own answers: a settings.yaml of its own, read like the real one, written by the answer.
    path = tmp_path / "settings.yaml"
    assert session_rules(path) == {}
    write_session_rules(path, {"Slack": "look"})
    assert session_rules(path) == {"Slack": Level.LOOK} and path.read_text().startswith("# This session's own answers")


def test_the_agents_rules_follow_their_files_and_carry_its_asks(tmp_path, monkeypatch):
    """Rules: the permissions read again when a watched file changes (a half-written one keeps the last); why a call is refused, for the
    model; and an ask, written to the session's asks file and answered by a rule or a denial."""
    settings, own, asks = tmp_path / "settings.yaml", tmp_path / "own.yaml", tmp_path / "asks.jsonl"
    loads = []
    def load():
        loads.append(1)
        return Permissions(Level.LOOK, {**rules(settings.read_text() if settings.exists() else "", "apps"), **session_rules(own)}, {"chase.com": Level.BLOCK})
    given = Rules(load, (settings, own), asks)
    assert given.refusal("Slack", "", None, "use") == "Slack is look only by the person's permissions: it may be read, not acted in. If the task needs it, ask once: ax.ask('Slack', 'use', why='what for')."
    assert given.refusal("Slack", "", None, "look") is None and loads == [1]  # nothing changed: not read again
    settings.write_text("Slack: use")
    assert given.refusal("Slack", "", None, "use") is None and loads == [1, 1]
    settings.write_text("Slack")  # half-written: the last rules hold, and the next change is read
    assert given.refusal("Slack", "", None, "use") is None and loads == [1, 1, 1]
    settings.write_text("Slack: use, Messages: block")
    assert given.refusal("Messages", "", None, "look") == "Messages is blocked by the person's permissions: do without it, or say in your reply that the task needs it."
    assert given.ask("Messages", "", "look", why="read the thread") == "Messages: blocked by the person's permissions, so not asked for. Do without it, or say in your reply that the task needs it."
    assert not asks.exists()  # a block is the person's answer already: nothing asked
    bar = [SimpleNamespace(role="AXTextField", value="https://online.chase.com/login")]
    assert given.refusal("Safari", "com.apple.Safari", bar, "look") == "The page open in Safari is blocked by the person's permissions (a site rule): use another page, or tell the person."
    assert given.refusal("Notes", "com.apple.Notes", bar, "look") is None  # a URL in another app's field is content
    assert Rules(lambda: Permissions(ASK), ()).refusal("Finder", "", None, "look").startswith("Finder is not allowed yet by the person's permissions: apps not listed are asked about.")
    # An ask: written, then waited on; the person's answer is a rule (Allow, Allow in this session) or a denial (Block).
    answers = iter([lambda: write_session_rules(own, {"Notes": "use"}), lambda: answer_ask(asks, "Mail")])
    monkeypatch.setattr(permissions, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: next(answers)()))
    assert given.ask("Slack", "", "use", "reply") == "Slack: already allowed."
    assert given.ask("Notes", "com.apple.Notes", "use", why="type the list") == "Notes: allowed."
    assert given.ask("Mail", "com.apple.mail", "use", why="send the thread") == "Mail: denied for this task. Do without it, or say in your reply that the task needs it."
    assert given.ask("Mail", "com.apple.mail", "use", why="again", timeout_s=0) == "Mail: denied for this task already, so not asked again. Do without it, or say in your reply that the task needs it."
    assert given.refusal("Mail", "com.apple.mail", None, "use") == "Mail is look only by the person's permissions: it may be read, not acted in. The person denied more of it for this task: do without it, or say in your reply that the task needs it."
    assert given.ask("Finder", "com.apple.finder", "use", why="move the file", timeout_s=0) == "Finder: no answer yet. Continue without it, or tell the person what it was for."
    rows = read_jsonl(asks)[0]
    assert [(row["app"], row.get("level"), row.get("why"), row.get("denied")) for row in rows] == [
        ("Notes", "use", "type the list", None), ("Mail", "use", "send the thread", None), ("Mail", None, None, True), ("Finder", "use", "move the file", None)]
    assert pending_asks(asks, given.current()) == [{"app": "Finder", "bundle": "com.apple.finder", "ts": rows[-1]["ts"], "level": "use", "why": "move the file", "source": "agent"}]
    answer_ask(asks, "Finder")
    assert pending_asks(asks, given.current()) == []
    time.sleep(0.002)
    given.ask("Finder", "com.apple.finder", "use", why="again", timeout_s=0)  # a denial answers the asks before it, not a later one
    assert [item["why"] for item in pending_asks(asks, given.current())] == ["again"]
    # Allow once: an answer row and no rule; the grant serves the one cell that uses it (as Claude Code's Yes serves one call), and a
    # page's site rule still counts.
    answers = iter([lambda: answer_ask(asks, "Calendar", "use")])
    assert given.ask("Calendar", "com.apple.iCal", "use", why="reply now") == "Calendar: allowed once, for one exec_py call that uses it; ask again after."
    given.cell_done()  # the asking cell ends without using it: the grant waits for the cell that does
    assert given.refusal("Calendar", "com.apple.iCal", None, "use") is None and Rules(load, (settings, own), asks).refusal("Calendar", "", None, "use") is not None
    given.cell_done()
    assert given.refusal("Calendar", "com.apple.iCal", None, "use") is not None
    answers = iter([lambda: answer_ask(asks, "Calendar", "use", "task")])
    assert given.ask("Calendar", "com.apple.iCal", "use", why="reply now") == "Calendar: allowed for this task."
    given.cell_done()
    assert given.refusal("Calendar", "com.apple.iCal", None, "use") is None  # for the rest of the run
    assert [item["app"] for item in pending_asks(asks, given.current())] == ["Finder"]
    given.allowed["Safari"] = Level.USE
    assert given.refusal("Safari", "com.apple.Safari", bar, "look") is not None
    assert Permissions(ASK, {"Slack": Level.USE, "Mail": Level.LOOK, "1Password*": Level.BLOCK}, {"chase.com": Level.BLOCK}).describe() == \
        "apps not listed: ask; use: Slack; look: Mail; block: 1Password*, site chase.com"


def test_the_live_worker_builds_ax_with_the_sessions_rules(monkeypatch, tmp_path):
    from app.live.actor.desktop import worker

    monkeypatch.delenv("CUA_SESSION_FOLDER", raising=False)
    assert worker.session_rules() is None  # the scenario page, or a worker started by hand: no rules
    monkeypatch.setenv("CUA_SESSION_FOLDER", str(tmp_path))
    given = worker.session_rules()
    assert isinstance(given, Rules) and given.asks == tmp_path / "asks.jsonl" and given.paths[1] == tmp_path / "settings.yaml"
    given.allowed["Mail"], given.step, given.spent = Level.USE, {"Mail"}, {"Mail"}
    monkeypatch.setattr(worker, "emit", lambda payload: None)
    worker.serve_requests({"ax": SimpleNamespace(rules=given)}, iter(['{"id": 1, "operation": "execute", "code": "x = 1"}']))
    assert given.allowed == {} and given.spent == set()  # each cell's end is reported: an Allow once served it alone
