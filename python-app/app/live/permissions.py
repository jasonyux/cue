"""What the system may do with each app and site: ``block`` (not even looked at), ``look`` (observed and read) or ``use``
(acted in, which includes looking). One rule per app name, bundle id or site, in settings.yaml under ``permissions``;
the strictest matching rule wins and ``default`` stands for anything no rule names. The observer applies them per look
(observer/sources.py); the agent's ``ax`` applies them per call through ``Rules`` (desktop/ax/client.py), which also
carries the agent's asks for more, answered on the console.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from io import StringIO
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from ruamel.yaml import YAML

from .utils.jsonl import append_jsonl, read_jsonl
from .utils.time import timestamp


class Level(StrEnum):
    """What may be done with an app or site; each level includes the ones before it."""

    BLOCK = "block"  # not even looked at
    LOOK = "look"  # observed and read
    USE = "use"    # acted in


class Reason(StrEnum):
    """What set a level (``Permissions.decide``), kept as the stub's ``reason`` on a look the observer did not read."""

    APP = "app"          # an app rule
    SITE = "site"        # a site rule
    DEFAULT = "default"  # no rule matched
    ASK = "ask"          # no rule matched and the default is ask: blocked until the person answers, and asked


LEVELS = tuple(Level)  # strictest first
ASK: Final = "ask"  # what ``default`` alone may hold (never a rule): an app no rule names is blocked, and asked about, until the person answers
Default = Level | Literal["ask"]
# The apps whose address bar site rules read, by bundle id prefix: Safari, Chrome and its channels, Chromium, Firefox and its editions,
# Vivaldi, Brave, Edge, Arc and Dia, Opera and Opera GX, Orion, Zen, Tor Browser. A URL in any other app's text field is content.
BROWSERS = ("com.apple.safari", "com.google.chrome", "org.chromium.chromium", "org.mozilla.firefox", "com.vivaldi.vivaldi", "com.brave.browser",
            "com.microsoft.edgemac", "company.thebrowser.", "com.operasoftware.opera", "com.kagi.kagimacos", "app.zen-browser.zen", "org.torproject.torbrowser")
# A browser's address bar as its accessibility value: a host with a dot, maybe a scheme, a port and a path; never a search.
ADDRESS = re.compile(r"^(?:https?://)?[\w-]+(?:\.[\w-]+)+(?::\d+)?(?:/\S*)?$")


class Field(Protocol):
    role: str
    value: str


def level_of(value: Any, key: str) -> Level:
    try:
        return Level(str(value).strip().lower())
    except ValueError:
        raise ValueError(f"{key} must be one of {', '.join(Level)}, not {value!r}.") from None


def default_of(value: Any, key: str) -> Default:
    """What ``permissions.default`` may hold: a level, or ask."""
    text = str(value).strip().lower()
    if text == ASK:
        return ASK
    try:
        return Level(text)
    except ValueError:
        raise ValueError(f"{key} must be one of {ASK}, {', '.join(Level)}, not {value!r}.") from None


def session_rules(path: Path) -> dict[str, Level]:
    """A session's own answers, ``permissions.apps`` in the settings.yaml of its folder: what "This session" allowed."""
    if not path.exists():
        return {}
    data = YAML(typ="safe").load(path.read_text()) or {}
    return rules((data.get("permissions") or {}).get("apps") or {}, "the session's permissions.apps")


def write_session_rules(path: Path, apps: dict[str, str]) -> None:
    out = StringIO()
    YAML().dump({"permissions": {"apps": apps}}, out)
    path.write_text("# This session's own answers to permission questions (\"This session\" on the Zen page); gone with the session.\n" + out.getvalue())


def pending_asks(path: Path, permissions: Permissions) -> list[dict[str, Any]]:
    """The agent's asks in a session's asks file with no answer yet, newest first, one per app: an ask is answered by a
    rule that grants its level (a rule by hand) or by an answer row written after it (Allow once, Allow for this task, Block)."""
    rows = read_jsonl(path)[0]
    answered = {str(row.get("app")): str(row.get("ts") or "") for row in rows if row.get("denied") or row.get("allowed")}
    asks: dict[str, dict[str, Any]] = {}
    for row in rows:
        app, ts = str(row.get("app") or ""), str(row.get("ts") or "")
        if row.get("denied") or row.get("allowed") or answered.get(app, "") >= ts or not app:
            continue
        if LEVELS.index(permissions.level(app, str(row.get("bundle") or ""))) < LEVELS.index(level_of(row.get("level"), "ask level")):
            asks[app] = {"app": app, "bundle": str(row.get("bundle") or ""), "ts": ts, "level": str(row.get("level")), "why": str(row.get("why") or ""), "source": "agent"}
    return sorted(asks.values(), key=lambda item: item["ts"], reverse=True)


def answer_ask(path: Path, app: str, allowed: str | None = None, scope: str = "once") -> None:
    """An answer that writes no rule: Block (a denial row), or the level allowed for one step (scope ``once``) or the rest of the
    asking run (``task``)."""
    append_jsonl(path, {"ts": timestamp(), "app": app, **({"allowed": allowed, "scope": scope} if allowed else {"denied": True})})


def rules(value: Any, key: str) -> dict[str, Level]:
    """A mapping of pattern → level, or the page's text "Slack: look, Code: use" → the same mapping."""
    if isinstance(value, str):
        value = dict(item.partition(":")[::2] for item in value.split(",") if item.strip())
    if not isinstance(value, dict) or not all(isinstance(name, str) and name.strip() for name in value):
        raise ValueError(f"{key} must be entries of the form name: level (block, look or use), comma-separated.")
    return {name.strip(): level_of(level, f"{key} {name.strip()}") for name, level in value.items()}


def matches(pattern: str, subject: str) -> bool:
    return bool(subject) and fnmatchcase(subject.lower(), pattern.lower())


def matches_site(pattern: str, address: str) -> bool:
    """A bare domain covers itself and its subdomains; a pattern with * or / is a glob against the address."""
    pattern, address = pattern.lower(), address.lower()
    if "*" in pattern or "/" in pattern:
        return fnmatchcase(address, pattern) or fnmatchcase(address, f"*.{pattern}")
    host = address.split("/", 1)[0].rsplit(":", 1)[0]
    return host == pattern or host.endswith(f".{pattern}")


def browser(bundle: str) -> bool:
    return bundle.lower().startswith(BROWSERS)


def address(elements: Sequence[Field]) -> str | None:
    """A browser's address bar, the first text field holding a URL, as the bar shows it (Chrome drops the scheme); None elsewhere."""
    for element in elements:
        if element.role == "AXTextField" and ADDRESS.match(element.value.strip()):
            return re.sub(r"^https?://", "", element.value.strip())
    return None


@dataclass(frozen=True)
class Permissions:
    default: Default = Level.LOOK  # for an app no rule names: a level, or ask
    apps: dict[str, Level] = field(default_factory=dict)   # app name or bundle id, globs allowed → level
    sites: dict[str, Level] = field(default_factory=dict)  # a domain (itself and its subdomains) or a glob on the address → level

    def decide(self, name: str, bundle: str = "", address: str | None = None) -> tuple[Level, Reason]:
        """The strictest level among the rules that match and what set it. Between an app rule and a site rule at the
        same level, the app rule: the page is never read when the app itself is none."""
        found = [(level, Reason.APP) for pattern, level in self.apps.items() if matches(pattern, name) or matches(pattern, bundle)]
        if address:
            found += [(level, Reason.SITE) for pattern, level in self.sites.items() if matches_site(pattern, address)]
        if found:
            return min(found, key=lambda item: LEVELS.index(item[0]))
        return (Level.BLOCK, Reason.ASK) if self.default == ASK else (Level(self.default), Reason.DEFAULT)

    def level(self, name: str, bundle: str = "", address: str | None = None) -> Level:
        return self.decide(name, bundle, address)[0]

    def describe(self) -> str:
        """The rules as the agent is told them: the default, then the apps and sites per level, strictest last."""
        parts = [f"apps not listed: {self.default}"]
        for level in reversed(LEVELS):
            names = [name for name, held in self.apps.items() if held == level] + [f"site {name}" for name, held in self.sites.items() if held == level]
            if names:
                parts.append(f"{level}: {', '.join(names)}")
        return "; ".join(parts)


ASK_POLL_S = 0.5
ASK_TIMEOUT_S = 600.0


class Rules:
    """The permissions as the agent's ``ax`` applies them: ``load`` reads them (the worker passes the settings' loader), read
    again whenever one of ``paths`` changes, so an answer on the console takes effect at the agent's next call. ``asks`` is
    the session's asks file: ``ask`` writes the agent's question there and waits for the answer, a rule, a denial, or an
    Allow once or Allow for this task, which lift the app's level for the one cell that uses it next (as Claude Code's Yes
    serves one call) or for the rest of the run, the process ``ax`` lives in, and write no rule; the worker reports each
    cell's end with ``cell_done``. A denial holds for the rest of the run too: the app is not asked for again."""

    def __init__(self, load: Callable[[], Permissions], paths: Sequence[Path], asks: Path | None = None) -> None:
        self.load, self.paths, self.asks = load, tuple(paths), asks
        self.permissions = Permissions()
        self.seen: tuple[int | None, ...] | None = None
        self.allowed: dict[str, Level] = {}  # app → the level the person allowed without a rule, once or for this task
        self.step: set[str] = set()  # the apps allowed once: for one cell
        self.spent: set[str] = set()  # the grants the cell under way has used
        self.denied: set[str] = set()  # the apps the person denied: for the rest of the run

    def current(self) -> Permissions:
        stamp = tuple(path.stat().st_mtime_ns if path.exists() else None for path in self.paths)
        if stamp != self.seen:
            with suppress(Exception):  # a half-written file: the last rules hold, read again at the next call
                self.permissions = self.load()
                self.seen = stamp
        return self.permissions

    def refusal(self, name: str, bundle: str, elements: Sequence[Field] | None, need: str) -> str | None:
        """Why the app may not be read (``need`` look) or acted in (use), for the model, or None. With the elements of a
        browser's window, the page's site rules count too."""
        wanted = level_of(need, "need")
        site = address(elements) if elements and browser(bundle) else None
        level, reason = self.current().decide(name, bundle, site)
        if reason != Reason.SITE and LEVELS.index(self.allowed.get(name, level)) > LEVELS.index(level):  # a grant lifts the app's rule, not a page's
            level = self.allowed[name]
            self.spent.add(name)
        if LEVELS.index(level) >= LEVELS.index(wanted):
            return None
        if reason == Reason.SITE:
            return f"The page open in {name} is {'blocked' if level == Level.BLOCK else 'look only'} by the person's permissions (a site rule): use another page, or tell the person."
        if level == Level.BLOCK and reason != Reason.ASK:
            return f"{name} is blocked by the person's permissions: do without it, or say in your reply that the task needs it."
        state = ("not allowed yet by the person's permissions: apps not listed are asked about" if reason == Reason.ASK
                 else "look only by the person's permissions: it may be read, not acted in")
        if name in self.denied:
            return f"{name} is {state}. The person denied more of it for this task: do without it, or say in your reply that the task needs it."
        return f"{name} is {state}. If the task needs it, ask once: ax.ask({name!r}, {str(wanted)!r}, why='what for')."

    def ask(self, name: str, bundle: str, need: str, why: str, timeout_s: float = ASK_TIMEOUT_S) -> str:
        """Ask the person for ``need`` on the app, with what it is for, and wait for the answer: allowed (a rule now grants
        it, or they allowed it once or for this task), denied, or none within the timeout. A blocked app is not asked for:
        the person said no already. The words are for the model."""
        if self.refusal(name, bundle, None, need) is None:
            return f"{name}: already allowed."
        level, reason = self.current().decide(name, bundle)
        if level == Level.BLOCK and reason != Reason.ASK:
            return f"{name}: blocked by the person's permissions, so not asked for. Do without it, or say in your reply that the task needs it."
        if name in self.denied:
            return f"{name}: denied for this task already, so not asked again. Do without it, or say in your reply that the task needs it."
        if self.asks is None:
            return "No one to ask: this desktop is not in a session."
        answers_from = len(read_jsonl(self.asks)[0]) + 1  # the rows after this ask's own: the file only grows
        append_jsonl(self.asks, {"ts": timestamp(), "app": name, "bundle": bundle, "level": str(level_of(need, "need")), "why": why})
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(ASK_POLL_S)
            if self.refusal(name, bundle, None, need) is None:
                return f"{name}: allowed."
            for row in read_jsonl(self.asks)[0][answers_from:]:
                if row.get("app") != name:
                    continue
                if row.get("denied"):
                    self.denied.add(name)
                    return f"{name}: denied for this task. Do without it, or say in your reply that the task needs it."
                if row.get("allowed"):
                    self.allowed[name] = level_of(row["allowed"], "allowed")
                    if row.get("scope") == "task":
                        self.step.discard(name)
                        return f"{name}: allowed for this task."
                    self.step.add(name)
                    return f"{name}: allowed once, for one exec_py call that uses it; ask again after."
        return f"{name}: no answer yet. Continue without it, or tell the person what it was for."

    def cell_done(self) -> None:
        """A cell ended: the Allow once grants it used are gone, so the next cell that needs them asks again."""
        for name in self.spent & self.step:
            del self.allowed[name]
            self.step.discard(name)
        self.spent.clear()
