"""The records that cross a stage boundary or land on disk. One model per file in the session folder.

Every stage reads the previous stage's records and writes its own, so these shapes are the contract between
``observer/``, ``proposer/``, ``actor/``, and the console. Validation happens on read (``parse_stored``), so
a wrong shape fails loudly at the file boundary instead of corrupting the next stage.

Keys on disk and on the wire are camelCase (``windowStart``), the Python fields snake_case; ``Snapshot`` keeps
the snake_case keys the observer has always written. ``wire()`` drops unset optional fields except the few that
the console and older files expect as explicit nulls.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, field_validator, model_serializer
from pydantic.alias_generators import to_camel

from ..config import MAX_PROPOSALS_CAP

Kind = Literal["explain", "lookup", "act"]
KINDS: tuple[str, ...] = ("explain", "lookup", "act")


def new_id() -> str:
    """A record's id: random and short, the shape model calls and flags already have. It is minted before the record is
    written, so the row in its own file and its event in the stream carry the same one."""
    return uuid4().hex[:12]


class LiveRecord(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")
    keep_none: ClassVar[frozenset[str]] = frozenset()  # wire keys written even when None

    def wire(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, mode="json")
        return {key: value for key, value in data.items() if value is not None or key in self.keep_none}

    @classmethod
    def parse_stored(cls, record: dict[str, Any]):
        return cls.model_validate(record)


# ----- observer → snapshots.jsonl ----------------------------------------------------------------------------

OBSERVER_ROW_KINDS = {"snapshot": "screen_changed", "same": "screen_unchanged", "screenshot": "screenshot_taken", "paused": "observer_paused"}
# the names observer rows had before the stream; files from then are read as today's


def row_kind(row: dict[str, Any]) -> str:
    """A snapshots.jsonl row's kind in today's names, for a reader that keeps the raw rows."""
    kind = str(row.get("kind") or "screen_changed")
    return OBSERVER_ROW_KINDS.get(kind, kind)


class Snapshot(LiveRecord):
    """One observer tick. ``kind`` is screen_changed (a new read of the focused window), screen_unchanged, screenshot_taken,
    observer_paused, observer_started, or observer_stopped: the same names as the stream's events."""

    model_config = ConfigDict(alias_generator=None, populate_by_name=True, extra="ignore")

    kind: str = "screen_changed"
    ts: str
    app: str = ""
    bundle: str = ""
    pid: int = 0
    window: str = ""
    tree: str = ""
    hash: str = ""
    excluded: bool = False              # the content was not read: see reason
    reason: str = ""                    # what set it, as Permissions.decide says: "app" (an app rule), "site" (a site rule), "default" (no rule)
    error: str | None = None
    pointer_app: str | None = None     # set only when the pointer is over a different app or window
    pointer_window: str | None = None
    visible: list[str] | None = None    # other on-screen windows, front to back, as "App — title"
    selection: str | None = None        # the text selected in the focused element, when there is any (a field's or a page's)
    around: str | None = None           # the lines around that selection, or the text cursor, in a focused text element (>>> <<< mark it)
    path: str | None = None             # screenshot records: file name under screenshots/

    @field_validator("kind", mode="before")
    @classmethod
    def todays_kind(cls, value: Any) -> Any:
        return OBSERVER_ROW_KINDS.get(value, value) if isinstance(value, str) else value


# ----- summarizer → summaries.jsonl ----------------------------------------------------------------------------

class Summary(LiveRecord):
    """One worklog line for one window of observations."""

    id: str = ""                  # its stream event's id too; "" on records from before ids were written
    ts: str = ""
    window_start: str = ""
    window_end: str = ""
    summary: str = ""
    observations: int = 0
    model: str = ""
    usage: dict[str, Any] = {}
    images: int = 0
    fallback: str | None = None   # "thinking_truncated": answered without thinking after the budget ran out
    error: str | None = None      # a tick that failed before producing a summary
    session_id: str | None = None  # set on the copy in data/memory/summaries.jsonl, which spans sessions
    call_id: str = ""              # the model call that produced it: its ``id`` in live_model_calls.jsonl


# ----- belief updater → data/memory/tasks.json, and beliefs.jsonl in the session ---------------------------------

TaskStatus = Literal["active", "done", "paused"]


class Step(LiveRecord):
    """One checklist item of a task; ``done_at`` set means finished (dropped after belief.done_step_days),
    ``deleted_at`` set means in the trash (dropped after belief.trash_days)."""

    text: str
    done_at: str | None = None
    deleted_at: str | None = None
    call_id: str = ""          # the call (or operator edit) that added it: its row in live_model_calls.jsonl (or events.jsonl)
    added_at: str = ""         # when it was added; "" on entries from before it was stored (storage/backfill_added.py fills them)


class Task(LiveRecord):
    """One thing the person is working on, as the belief updater currently understands it."""

    id: str
    title: str
    status: TaskStatus = "active"
    steps: list[Step] = []      # the checklist: pending items and recently finished ones
    last_seen: str = ""         # when a summary last touched it
    session_id: str = ""        # the session that last touched it
    evidence: list[str] = []    # worklog times the model cited, newest few
    deleted_at: str | None = None  # in the trash: hidden from the list, recoverable for belief.trash_days
    call_id: str = ""           # the call (or operator edit) that added it; "" on tasks from before ids were recorded


class StepSpec(LiveRecord):
    """A checklist entry to add: its text and whether it is already done."""

    text: str
    done: bool = False


class RewordSpec(LiveRecord):
    """A checklist entry to reword: its number on the list as shown, and its new text."""

    step: int
    text: str


class TaskOp(LiveRecord):
    """One change to the task list. ``add``: a task with its entries. ``update``: add entries, move entries to the
    trash by number and/or reword entries by number. ``status``: set the task (no ``step``) or one entry (``step``) done or not done. ``rename``: a
    new title. ``delete``: the task to the trash. ``restore``: the task (no ``step``) or one of its trashed entries
    (``step``, its number in the trash) back from the trash."""

    op: Literal["add", "update", "status", "rename", "delete", "restore"]
    task_id: str | None = None
    title: str | None = None
    steps: list[StepSpec] = []   # add / update: entries to add
    remove: list[int] = []       # update: entry numbers to trash
    reword: list[RewordSpec] = []  # update: entries to reword
    step: int | None = None      # status / restore: the entry number, or None for the task itself
    done: bool | None = None     # status: the state to set
    evidence: list[str] = []


def _texts(value: Any, limit: int = 10) -> list[str]:
    items = value if isinstance(value, list) else []
    return [str(v).strip() for v in items if isinstance(v, str | int | float) and str(v).strip()][:limit]


def _specs(value: Any) -> list[StepSpec]:
    """Entries as {"text", "done"} objects, or bare strings for pending ones."""
    specs = []
    for item in (value if isinstance(value, list) else [])[:10]:
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
            specs.append(StepSpec(text=item["text"].strip(), done=item.get("done") is True))
        elif isinstance(item, str) and item.strip():
            specs.append(StepSpec(text=item.strip()))
    return specs


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, int | str) and not isinstance(value, bool) and str(value).isdigit() else None


def _rewords(value: Any) -> list[RewordSpec]:
    """Entries to reword as {"step", "text"} objects."""
    specs = []
    for item in (value if isinstance(value, list) else [])[:10]:
        number = _number(item.get("step")) if isinstance(item, dict) else None
        if number is not None and isinstance(item.get("text"), str) and item["text"].strip():
            specs.append(RewordSpec(step=number, text=item["text"].strip()))
    return specs


def normalize_op(item: Any) -> TaskOp | None:
    if not isinstance(item, dict) or item.get("op") not in ("add", "update", "status", "rename", "delete", "restore"):
        return None
    text = {key: str(item[key]).strip() for key in ("task_id", "title") if isinstance(item.get(key), str) and str(item[key]).strip()}
    if item["op"] == "add" and "title" not in text or item["op"] != "add" and "task_id" not in text:
        return None
    if item["op"] == "rename" and "title" not in text:
        return None
    if item["op"] == "status" and not isinstance(item.get("done"), bool):
        return None
    return TaskOp(op=item["op"], task_id=text.get("task_id") if item["op"] != "add" else None, title=text.get("title"),
                  steps=_specs(item.get("add", item.get("steps"))),
                  remove=[n for n in (_number(v) for v in _texts(item.get("remove"))) if n is not None],
                  reword=_rewords(item.get("reword")),
                  step=_number(item.get("step")), done=item.get("done") if item["op"] == "status" else None,
                  evidence=_texts(item.get("evidence"), 5))


class BeliefCall(LiveRecord):
    """One tool call of a belief tick, for the timeline's task-update card: the round it came in, the op it asked for (None
    when the tool or its arguments were not understood), whether it was applied, and the result the model read."""

    round: int
    tool: str
    op: TaskOp | None = None
    applied: bool
    result: str


class BeliefUpdate(LiveRecord):
    """One belief tick: the ops the model's accepted tool calls asked for, over every round of the tick, and the task
    list after them when the row must carry it (``tasks``). ``rejected`` holds the result of each call the check refused (belief/tools.py);
    ``usage`` and ``call_id`` are the rounds' total and the last round's call."""

    id: str = ""                  # its stream event's id too; "" on records from before ids were written
    ts: str = ""
    window_start: str = ""
    window_end: str = ""
    ops: list[TaskOp] = []
    tasks: list[Task] | None = None  # the visible list after the tick, on a row whose ops do not rebuild it from the row before: a session's first, one after a rule fired or the operator edited (ops.replay); None otherwise
    rounds: int = 1             # model replies in this tick: one, plus one per reply that had a rejected or held call
    rejected: list[str] = []
    calls: list[BeliefCall] = []  # every tool call of the tick in order, with its result (belief/update.py)
    warned: list[str] = []      # the result of each call the lookalike check held back (belief/update.py); the same call again goes through
    embedding: dict[str, Any] | None = None  # the lookalike check's embeddings call this tick: {"model", "usage"}, for the cost panel
    observations: int = 0       # worklog lines read
    model: str = ""
    usage: dict[str, Any] = {}
    images: int = 0
    reasoning: str | None = None
    fallback: str | None = None
    raw: str | None = None
    call_id: str = ""           # the model call that produced it
    error: str | None = None


class PreferenceUpdate(LiveRecord):
    """One preference tick (preference/update.py): the file as the tick read it and as it left it (``after`` None when the
    model made no change), the reactions it read (their event ids), the history searches it made, and the writes the cap
    refused. ``usage`` and ``call_id`` are the rounds' total and the last round's call."""

    id: str = ""
    ts: str = ""
    window_start: str = ""
    window_end: str = ""
    before: str = ""
    after: str | None = None
    changed: bool = False
    rounds: int = 1
    rejected: list[str] = []
    searches: list[dict[str, Any]] = []
    feedback: list[str] = []    # the stream ids of the reactions read
    observations: int = 0       # how many of them
    model: str = ""
    usage: dict[str, Any] = {}
    images: int = 0
    reasoning: str | None = None
    fallback: str | None = None
    raw: str | None = None
    call_id: str = ""
    error: str | None = None


# ----- proposer → proposals.jsonl ----------------------------------------------------------------------------------

class Proposal(LiveRecord):
    kind: Kind = "act"
    task: str
    reason: str = ""
    confidence: float = 0.0   # 1 (a long shot) to 5 (sure they will accept); 0 when the model gave none
    human_seconds: float | None = None  # the model's rough estimate of the person's time to do it by hand; None when it gave none
    evidence: list[str] = []  # the ids of the lines it rests on, as the prompt showed them: worklog lines, decisions, chat turns, search results, tasks

    @model_serializer(mode="wrap")
    def _omit_missing_estimate(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """No estimate, no key: a proposal without one is written exactly as before (nested dumps keep their Nones)."""
        data: dict[str, Any] = handler(self)
        if self.human_seconds is None:
            data.pop("humanSeconds", None)
            data.pop("human_seconds", None)
        return data


def _human_seconds(value: Any) -> float | None:
    """The by-hand estimate as a positive number of seconds, or None when missing or not a number."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def normalize_proposal(item: Any) -> Proposal | None:
    """One proposal with every field typed, or None when it has no task. Extra keys (older ``action``,
    ``reversible``) are dropped: nothing runs from a proposal set, so the model is not asked to decide that."""
    if not isinstance(item, dict):
        return None
    task = item.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    kind = str(item.get("kind") or "act").strip().lower()
    try:
        value = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    confidence = min(5.0, max(1.0, value)) if value else 0.0  # the 1-5 scale the prompt asks for
    cited = item.get("evidence")
    evidence = list(dict.fromkeys(str(entry).strip() for entry in cited if isinstance(entry, str | int) and str(entry).strip()))[:12] if isinstance(cited, list) else []
    return Proposal(kind=kind if kind in KINDS else "act", task=task.strip(),  # type: ignore[arg-type]
                    reason=str(item.get("reason") or "").strip(), confidence=confidence,
                    human_seconds=_human_seconds(item.get("human_seconds", item.get("humanSeconds"))), evidence=evidence)


class FilteredProposal(Proposal):
    """A proposal the duplicate filter (proposer/dedup.py) kept from the person: what it matched and how closely."""

    score: float
    matched: str   # the reference text it duplicates
    source: str    # decision | not_now | session; stale: an idle round's proposal the person went on before it was ready
    note: str      # the reference as the console names it


class ProposalSet(LiveRecord):
    """Ranked proposals for one window. The flat ``kind``/``task``/``reason``/``confidence`` mirror the top
    proposal; ``action`` is "propose" when there is at least one and "none" otherwise. ``filtered`` holds the
    proposals the duplicate filter removed, with their scores."""

    keep_none = frozenset({"task"})

    id: str = ""                  # its stream event's id too; "" on records from before ids were written
    ts: str = ""
    window_start: str = ""
    window_end: str = ""
    summary: str = ""
    kind: str = "act"
    action: str = "none"
    task: str | None = None
    reason: str = ""
    confidence: float = 0.0
    proposals: list[Proposal] = []
    filtered: list[FilteredProposal] = []
    embedding: dict[str, Any] | None = None  # the duplicate filter's embeddings call this round: {"model", "usage"}, for the cost panel
    observations: int = 0
    model: str = ""
    usage: dict[str, Any] = {}
    images: int = 0
    reasoning: str | None = None
    fallback: str | None = None
    raw: str | None = None        # the model's text when it was not a JSON object
    effect: str | None = None     # policy: recorded, proposed, notified, or stale (an idle round the person went on before)
    trigger: str | None = None    # what started the round: "idle" for the idle trigger; None for the timer or "propose now"
    searches: list[dict[str, Any]] = []  # the history searches the model made this round: {query, since, until, hits, lines}
    cited: list[dict[str, Any]] = []     # the search results the proposals cite: {id, ts, kind, sessionId, line}, for the console to show one from another session
    call_id: str = ""             # the model call that produced it
    error: str | None = None

    @classmethod
    def from_model_output(cls, parsed: dict[str, Any], limit: int = MAX_PROPOSALS_CAP) -> ProposalSet:
        """Model output → proposal set. Accepts the ranked ``proposals`` shape and the older single-task shape."""
        limit = max(1, min(int(limit), MAX_PROPOSALS_CAP))
        candidates = parsed.get("proposals")
        if not isinstance(candidates, list):
            legacy_action = str(parsed.get("action") or "propose").strip().lower()
            candidates = [parsed] if legacy_action != "none" else []
        proposals: list[Proposal] = []
        for candidate in candidates:
            proposal = normalize_proposal(candidate)
            if proposal is None or any(proposal.task == seen.task for seen in proposals):
                continue
            proposals.append(proposal)
            if len(proposals) == limit:
                break
        top = proposals[0] if proposals else None
        return cls(
            summary=str(parsed.get("summary") or "").strip() or "(no summary)",
            kind=top.kind if top else "act",
            action="propose" if top else "none",
            task=top.task if top else None,
            reason=(top.reason if top else "") or str(parsed.get("reason") or "").strip(),
            confidence=top.confidence if top else 0.0,
            proposals=proposals,
        )

    @classmethod
    def parse_stored(cls, record: dict[str, Any]) -> ProposalSet:
        """A stored proposal set, including ones from before ranked proposals (a single flat task)."""
        data = dict(record)
        if "proposals" not in data and data.get("action") in ("propose", "do"):
            top = normalize_proposal(data)
            data["proposals"] = [top] if top else []
        return cls.model_validate(data)


# ----- operator feedback, run markers, model calls -----------------------------------------------------------

class ProposalFeedback(LiveRecord):
    """feedback.jsonl: accept one proposal (``choice`` is its index) or reject the whole proposal set. ``context`` is a
    digest of what the proposer saw that round, made at decision time; ``model`` and ``usage`` are that digest call's."""

    keep_none = frozenset({"choice", "task"})

    ts: str
    proposals_ts: str
    decision: Literal["accept", "reject"]
    choice: int | None = None
    kind: str | None = None
    action: str = "none"
    task: str | None = None
    reason: str = ""
    confidence: float = 0.0
    proposals: list[Proposal] = []
    model: str = ""
    context: str = ""
    usage: dict[str, Any] = {}
    reject_reason: str = ""   # given after a rejection, optionally: a console preset or the person's own words
    call_id: str = ""         # the proposer call that made the proposal set


class Decision(LiveRecord):
    """data/memory/feedback.jsonl: one accept or reject with what the person was doing at the time, across sessions;
    the proposer's ``feedback`` source. ``id`` is its event's: the prompt shows it, so a proposal can cite it."""

    id: str = ""
    ts: str
    session_id: str = ""
    decision: str = ""
    kind: str = ""
    task: str = ""
    context: str = ""
    reject_reason: str = ""
    reason_typed: bool = False  # the person typed the reason (the console's "other…") rather than clicking a preset
    call_id: str = ""        # the proposer call that made the proposal set

    @classmethod
    def from_feedback(cls, feedback: dict[str, Any], session_id: str) -> Decision:
        return cls(
            id=str(feedback.get("id") or ""),
            ts=feedback["ts"],
            session_id=session_id,
            decision=feedback["decision"],
            kind=feedback.get("kind") or "",
            task=feedback.get("task") or "",
            context=feedback.get("context") or "",
            reject_reason=feedback.get("rejectReason") or "",
            reason_typed=bool(feedback.get("reasonTyped")), call_id=feedback.get("callId") or ""
        )


class TaskFlag(LiveRecord):
    """data/memory/task_feedback.jsonl: the operator's thumbs up or down on a task (``step`` None) or on one of its
    entries (``step`` its number when flagged, ``text`` what it said: the entry is found again by its text, since the
    numbers shift), with the reason given after a thumbs down when there was one. One record per task or entry, found
    again by ``id``: a later click updates it or removes it. Nothing on the list changes; the events log holds the ops
    that produced the task."""

    keep_none = frozenset({"step"})

    id: str
    ts: str
    session_id: str = ""
    task_id: str
    title: str = ""
    step: int | None = None
    text: str = ""
    verdict: Literal["up", "down"] = "down"
    reason: str = ""
    call_id: str = ""              # copied from the task or entry: the call (or operator edit) that added it


class SummaryFeedback(LiveRecord):
    """summary_feedback.jsonl: a flag on a summary with an optional note; keeps enough to find the model call."""

    ts: str
    summary_ts: str
    verdict: Literal["wrong", "ok"] = "wrong"
    comment: str = ""
    window_start: str = ""
    window_end: str = ""
    summary: str = ""
    model: str = ""
    images: int = 0
    observations: int = 0


class RunMarker(LiveRecord):
    """runs.jsonl: a task run started from the session, and later its end."""

    ts: str
    run_id: str
    event: Literal["started", "finished"] = "started"
    prompt: str = ""
    live_context: str | None = None
    status: str | None = None
    chat: bool | None = None  # the run is a turn of the session's chat
    proposal: dict[str, Any] | None = None  # the accepted proposal the run carries out: {"proposalsTs", "choice"}


class ModelCall(LiveRecord):
    """live_model_calls.jsonl: every request and response of the live stages, each under a generated ``id`` that the
    record it produced, the ops it applied and the feedback on them carry. The window bounds are what a replay reads
    back from the stream; the settings the call ran with are in the session's session.json."""

    id: str = ""
    ts: str
    kind: str
    window_start: str | None = None
    window_end: str | None = None
    request: dict[str, Any] = {}
    response: dict[str, Any] = {}


class StreamEvent(LiveRecord):
    """data/memory/stream.jsonl: one envelope per thing that happened (``storage/stream.py`` lists the kinds). ``data``
    is the record as its own file holds it; ``session_id`` and ``call_id`` say where and from which model call."""

    id: str
    ts: str
    kind: str
    session_id: str = ""
    call_id: str = ""
    data: dict[str, Any] = {}


# ----- coercion at the edges: callers may still hand in plain dicts ----------------------------------------------

def as_snapshots(records: Sequence[Snapshot | dict[str, Any]]) -> list[Snapshot]:
    return [item if isinstance(item, Snapshot) else Snapshot.model_validate(item) for item in records]


def as_summaries(records: Sequence[Summary | dict[str, Any]]) -> list[Summary]:
    return [item if isinstance(item, Summary) else Summary.parse_stored(item) for item in records]


def as_proposal_sets(records: Sequence[ProposalSet | dict[str, Any]]) -> list[ProposalSet]:
    return [item if isinstance(item, ProposalSet) else ProposalSet.parse_stored(item) for item in records]


def as_tasks(records: Sequence[Task | dict[str, Any]]) -> list[Task]:
    return [item if isinstance(item, Task) else Task.parse_stored(item) for item in records]


def as_decisions(records: Sequence[Decision | dict[str, Any]]) -> list[Decision]:
    return [item if isinstance(item, Decision) else Decision.parse_stored(item) for item in records]
