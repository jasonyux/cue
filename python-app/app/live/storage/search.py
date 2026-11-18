"""The history index: the lines of the stream worth searching, in one SQLite full-text table, data/memory/search.sqlite,
so a stage can search weeks of worklog, chat, proposals and decisions in milliseconds, without reading the screen rows
that make up nearly all of the stream.

Derived from the stream, never written to directly. The index keeps a cursor into the stream and, before each search,
indexes what was appended since: the new lines, screened by their heads like ``Stream.events`` does, so a catch-up
costs milliseconds. Missing, or of an older ``VERSION``, it is rebuilt from the parts, about a second per 300 MB.

The tool a model calls it through is here too, independent of any stage: ``TOOL`` is its schema (OpenAI's function
shape; an MCP tool takes the same name, description and parameters), ``run_tool`` one call of it, the arguments as the
model gave them to the text it reads and a note of the search for the record. The proposer gives the tool to its
model (proposer/propose.py); an agent can be given the same, or reach it over ``GET /api/live/history``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..utils.time import date_clock
from .stream import HEAD, Cursor, Stream

VERSION = 2       # bump when what is indexed, or how, changes: the index is then rebuilt
LIMIT_CAP = 30    # results per search at most
DEFAULT_LIMIT = 10  # when a call names none
TEXT_CAP = 400    # characters of a line a result shows
KINDS = {         # the stream kinds indexed, and how a result names each
    "summary_written": "worklog",
    "chat_message": "you asked",
    "chat_reply": "assistant answered",
    "proposals_made": "proposed",
    "proposal_accepted": "accepted",
    "proposal_rejected": "declined",
    "reject_reason_given": "declined because",
}
GROUPS = {        # the kinds a search may be limited to: each a group of the stream kinds above
    "worklog": ("summary_written",),
    "chat": ("chat_message", "chat_reply"),
    "proposal": ("proposals_made",),
    "decision": ("proposal_accepted", "proposal_rejected", "reject_reason_given"),
}
DEFAULT_KINDS = ("worklog", "chat")  # what a search covers when it names no kinds: proposals are short lines and would rank high
WORD = re.compile(r"\w+\*?", re.UNICODE)  # a keyword, with its trailing * when it is a prefix
TOOL_NAME = "search_history"
TOOL: dict[str, Any] = {"type": "function", "function": {
    "name": TOOL_NAME,
    "description": "Search what the person did before: every worklog line, chat turn, proposal and decision from every "
                   "session, weeks back. Keywords all must match, by word stem, and a word may end in * to match any ending; "
                   "the best matches come first with their date and time. The worklog and the chat are searched unless kinds "
                   "says otherwise. Unsure of the wording, give a regex instead of or with keywords. Give since and/or until "
                   "to look at a time slice; with no keywords, the slice newest first.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Keywords, e.g. \"NeurIPS hotel\"; names, places, apps, documents, error text. neur* matches NeurIPS and neural."},
        "regex": {"type": "string", "description": "Optional: a regular expression the line must match, case-insensitive, e.g. \"neur(ips|al)|nips\" or \"hotel.*(sydney|melbourne)\"."},
        "kinds": {"type": "array", "items": {"type": "string", "enum": list(GROUPS)},
                  "description": "Optional: only these kinds, default worklog and chat. worklog: what the person did; chat: what they and "
                                 "the assistant said; proposal: what was suggested; decision: what they accepted or declined, and why."},
        "since": {"type": "string", "description": "Optional: an ISO date or timestamp in the local time the results show, e.g. 2026-10-14 or 2026-10-14T09:00."},
        "until": {"type": "string", "description": "Optional: an ISO date or timestamp, local time."},
        "limit": {"type": "integer", "description": f"Results at most, default {DEFAULT_LIMIT}, up to {LIMIT_CAP}."}},
        "required": []}}}


def text_of(kind: str, data: dict[str, Any]) -> str:
    """What of an event is searchable; "" for nothing (the line is then not indexed)."""
    if kind == "summary_written":
        return str(data.get("summary") or "")
    if kind in ("chat_message", "chat_reply"):
        return str(data.get("text") or "")
    if kind == "proposals_made":
        return "; ".join(str(item.get("task") or "") for item in data.get("proposals") or [] if isinstance(item, dict))
    if kind in ("proposal_accepted", "proposal_rejected"):
        task, reason = str(data.get("task") or ""), str(data.get("rejectReason") or "")
        return f"{task} ({reason})" if task and reason else task
    if kind == "reject_reason_given":
        return str(data.get("reason") or "") if data.get("typed") else ""  # the person's own words; a preset says nothing
    return ""


@dataclass(frozen=True)
class Hit:
    id: str  # the event's: what a model cites
    ts: str
    kind: str
    session_id: str
    text: str

    def line(self) -> str:
        return f"[{self.id}] [{date_clock(self.ts)}] {KINDS[self.kind]}: {self.text[:TEXT_CAP]}"

    def wire(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "kind": self.kind, "sessionId": self.session_id, "text": self.text, "line": self.line()}


def render(hits: list[Hit]) -> str:
    """The results as the model reads them."""
    return "\n".join(hit.line() for hit in hits) or "(nothing found)"


def run_tool(index: HistoryIndex, arguments: Any, *, before: str | None = None) -> tuple[str, dict[str, Any]]:
    """One call of the tool, from any stage or agent: the arguments as the model gave them (a dict, or anything else for
    none) → the text the model reads (the results, or why the call was rejected), and a note of the search for the
    record: {query, regex, kinds, since, until, hits, lines}. ``before``: a timestamp the results stay strictly before,
    whatever the arguments say (an agent's own turn is in the stream before it runs: its server gives its start)."""
    args = arguments if isinstance(arguments, dict) else {}
    query, regex = str(args.get("query") or ""), str(args.get("regex") or "") or None
    wanted = args.get("kinds") or []
    kinds = ([wanted] if isinstance(wanted, str) else [str(kind) for kind in wanted]) or list(DEFAULT_KINDS)
    since, until = str(args.get("since") or "") or None, str(args.get("until") or "") or None
    try:
        limit = int(args.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    note: dict[str, Any] = {"query": query, "regex": regex, "kinds": kinds, "since": since, "until": until}
    try:
        hits = index.search(query, regex=regex, kinds=kinds, since=since, until=until, before=before, limit=limit)
    except ValueError as error:
        return f"Rejected: {error}.", {**note, "hits": 0, "lines": [], "found": [], "error": str(error)}
    return render(hits), {**note, "hits": len(hits), "lines": [hit.line() for hit in hits[:5]],
                          "found": [hit.wire() for hit in hits]}  # every hit, for the caller to keep the cited ones; not for the record as it is


def bound(value: str | None, *, end: bool) -> str | None:
    """A time bound as the model gives it, as the stream's UTC timestamp: an ISO timestamp, or a date alone, which spans
    its whole day; local time, as the results show it, unless it carries a zone. ValueError when it is neither."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO date or timestamp") from None
    if len(value) == 10 and end:
        moment += timedelta(days=1) - timedelta(milliseconds=1)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")  # naive: local


class HistoryIndex:
    def __init__(self, path: Path, stream: Stream) -> None:
        self.path = path
        self.stream = stream
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)  # autocommit; catch_up takes its own lock
        self.db.create_function("regexp", 2, lambda pattern, text: re.search(pattern, text, re.IGNORECASE) is not None, deterministic=True)  # text REGEXP ?
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        if self._meta("version") != str(VERSION):
            self.db.execute("DROP TABLE IF EXISTS history")
            self.db.execute("DELETE FROM meta")
            self.db.execute("INSERT INTO meta VALUES ('version', ?)", (str(VERSION),))
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS history USING fts5("
                        "text, id UNINDEXED, kind UNINDEXED, ts UNINDEXED, session_id UNINDEXED, tokenize='porter unicode61')")

    def close(self) -> None:
        self.db.close()

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def cursor(self) -> Cursor:
        """Where in the stream the index has read up to."""
        return Cursor(self._meta("part") or "", int(self._meta("offset") or 0))

    def catch_up(self) -> int:
        """Index what the stream gained since the cursor; the number of lines added. Under the file's write lock from
        reading the cursor to writing it, so two processes over the same file never index a line twice."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            added = self._catch_up()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return added

    def _catch_up(self) -> int:
        cursor = self.cursor()
        rows: list[tuple[str, str, str, str, str]] = []
        for part in self.stream.parts():
            if part.stem < cursor.part:
                continue
            position = cursor.offset if part.stem == cursor.part else 0
            with part.open("rb") as handle:
                handle.seek(position)
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break  # a line still being written: next time
                    position += len(raw)
                    line = raw.decode("utf-8", errors="replace")
                    head = HEAD.match(line)
                    if head is None or head.group(2) not in KINDS:
                        continue
                    ts, kind, session_id = head.groups()
                    record = json.loads(line)
                    text = text_of(kind, record.get("data") or {})
                    if text.strip():
                        rows.append((text, str(record.get("id") or ""), kind, ts, session_id))
            cursor = Cursor(part.stem, position)
        self.db.executemany("INSERT INTO history (text, id, kind, ts, session_id) VALUES (?, ?, ?, ?, ?)", rows)
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("part", cursor.part), ("offset", str(cursor.offset))])
        return len(rows)

    def span(self) -> tuple[int, str]:
        """How many lines the index holds, and the date of the oldest."""
        self.catch_up()
        count, oldest = self.db.execute("SELECT count(*), min(ts) FROM history").fetchone()
        return int(count or 0), date_clock(oldest)[:10] if oldest else ""

    def search(self, query: str, *, regex: str | None = None, kinds: Sequence[str] = (), since: str | None = None,
               until: str | None = None, before: str | None = None, limit: int = 20) -> list[Hit]:
        """The best matches for ``query`` (every word must match, by stem, a word ending in * by prefix; with several
        words and no match, any of them) within the time slice, of the ``kinds`` asked for (GROUPS; DEFAULT_KINDS when none) and
        matching ``regex`` (case-insensitive) when given; ranked, a text once per kind (a proposal repeated round after
        round is one line; its decision is another), at most ``limit``, all strictly before the stream timestamp
        ``before`` when given. No words: the slice, newest first. ValueError for a bound that is not a date or
        timestamp, a kind that is not a group or a regex that does not compile."""
        self.catch_up()
        limit = max(1, min(int(limit), LIMIT_CAP))
        terms = [f'"{word.rstrip("*")}"' + ("*" if word.endswith("*") else "") for word in WORD.findall(query)]
        where, args = [], []
        for clause, value in (("ts >= ?", bound(since, end=False)), ("ts <= ?", bound(until, end=True)), ("ts < ?", before)):
            if value:
                where.append(clause)
                args.append(value)
        if unknown := [kind for kind in kinds if kind not in GROUPS]:
            raise ValueError(f"no such kind {', '.join(unknown)}; the kinds are {', '.join(GROUPS)}")
        names = [name for kind in kinds or DEFAULT_KINDS for name in GROUPS[kind]]
        where.append(f"kind IN ({', '.join('?' * len(names))})")
        args += names
        if regex:
            try:
                re.compile(regex)
            except re.error as error:
                raise ValueError(f"the regex does not compile: {error}") from error
            where.append("text REGEXP ?")
            args.append(regex)
        matches = [" AND ".join(terms), " OR ".join(terms)] if len(terms) > 1 else [" ".join(terms)]
        for match in matches:
            sql = "SELECT id, ts, kind, session_id, text FROM history WHERE " + " AND ".join([*(["history MATCH ?"] if match else []), *where] or ["1"])
            sql += " ORDER BY rank, ts DESC" if match else " ORDER BY ts DESC"
            rows = self.db.execute(sql + " LIMIT ?", [*([match] if match else []), *args, limit * 4]).fetchall()
            hits: list[Hit] = []
            seen: set[tuple[str, str]] = set()
            for event_id, ts, kind, session_id, text in rows:
                key = (kind, " ".join(text.lower().split()))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(Hit(event_id, ts, kind, session_id, text))
                if len(hits) == limit:
                    break
            if hits:
                return hits
        return []
