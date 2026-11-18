"""LiveSessionManager: start/stop a continuous session, run the summarizer and proposer on timers, feed task runs.

Orchestration only. Record shapes and feedback live in ``storage/``, the stages in ``observer/`` and ``proposer/``,
and the boundary to the task agent in ``actor/``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from ..errors import RunnerCoreError
from ..usage import record_usage
from .actor.chat import Chat, ChatStore, read_only, stored_chat
from .actor.cmux.browser import workspace as browser_workspace
from .actor.cmux.run import close_workspace
from .actor.prompts import ActorInputs
from .actor.session_runs import SessionRuns
from .actor.standalone.browser import AgentBrowser
from .actor.standalone.remote_view import RemoteView
from .belief.update import BeliefUpdater
from .config import MAX_PROPOSALS_CAP, LiveSettings, ModelEndpoint
from .observer.recorder_process import ObserverProcess
from .observer.summarize import Summarizer
from .permissions import Reason, answer_ask, pending_asks, session_rules, write_session_rules
from .preference.update import PreferenceUpdater
from .proposer.feedback_digest import feedback_digest
from .proposer.idle import OFF, IdleTrigger, ScreenWatch, action_idle_s, input_idle_s, screen_locked
from .proposer.notify import notify_proposals
from .proposer.propose import Proposer
from .proposer.quick import QuickProposer, stale
from .storage.disk import disk_report
from .storage.feedback import (
    REJECT_REASONS,
    annotated_proposals,
    annotated_summaries,
    feedback_record,
    record_sent,
    record_summary_feedback,
    reject_reason,
)
from .storage.memory import MemoryStore
from .storage.pack import Packing, compressed, screenshot
from .storage.paths import SessionPaths, new_session_id, session_paths
from .storage.records import (
    BeliefUpdate,
    Decision,
    PreferenceUpdate,
    ProposalSet,
    Summary,
    TaskFlag,
    new_id,
    normalize_op,
    row_kind,
)
from .storage.search import HistoryIndex
from .storage.sessions import (
    normalize_record,
    previous_sessions,
    screenshot_rows,
    stored_paths,
    task_changes,
    tasks_left,
)
from .storage.usage import usage_by_day, usage_rows
from .utils.embed import EmbeddingClient
from .utils.jsonl import amend_jsonl, append_jsonl, read_json, read_jsonl, write_json
from .utils.llm import LiveModelClient
from .utils.time import plus_seconds, seconds_between, shifted, timestamp

logger = logging.getLogger(__name__)
# What the landing page changes in a running session (tune()): the idle switch's keys and the two sliders'.
TUNABLE = ("proposerIntervalS", "proposerIdleTrigger", "proposerIdleS", "proposerIdleCooldownS", "proposerDedupThreshold")

ObserverFactory = Callable[[SessionPaths, LiveSettings, Path], Awaitable[Any]]  # the third argument: the stream's folder
ClientFactory = Callable[[ModelEndpoint], Any]
MAX_SESSION_REASON = "max session time reached"
RESUME_GRACE_S = 60.0  # a session resumed with less than this left gets this much
IDLE_POLL_S = 0.5      # how often the idle trigger reads the time since the last input
LOOK_WAIT_S = 4.0      # how long an idle round waits for the observer's fresh look (it naps at most 0.5s, a look takes ~1s)
FRAME_WAIT_S = 2.0     # and then for that look's frame (a screenshot of the window, saved just after the look)


class LiveSession:
    def __init__(self, session_id: str, paths: SessionPaths, settings: LiveSettings, summary_client: Any,
                 belief_client: Any, proposer_client: Any, memory: MemoryStore, index: HistoryIndex | None = None,
                 preference_client: Any = None, chats: ChatStore | None = None) -> None:
        self.id = session_id
        self.paths = paths
        self.settings = settings
        self.preference_client = preference_client  # the preference updater's (preference/update.py)
        self.clients = [summary_client, belief_client, proposer_client, *([preference_client] if preference_client is not None else [])]
        self.started_at = timestamp()
        self.stopped_at: str | None = None
        self.stop_reason: str | None = None
        self.expires_at: str | None = None
        self.observer: Any = None
        # record_usage() stores totals under "usage" of the dict it is given, like a run record.
        self.usage: dict[str, Any] = {}
        self.summarizer = Summarizer(paths, summary_client, settings, stream=memory.stream, session_id=session_id, usage=self.usage)
        belief_embedder = EmbeddingClient(settings.embed, memory.embeddings) if settings.belief_similar_threshold and settings.embed.model else None
        self.belief = BeliefUpdater(paths, belief_client, settings, memory, stream=memory.stream, session_id=session_id, usage=self.usage,
                                    embedder=belief_embedder)
        if belief_embedder is not None:
            self.clients.append(belief_embedder)
        embedder = EmbeddingClient(settings.embed, memory.embeddings) if settings.proposer_dedup else None
        self.proposer = Proposer(paths, proposer_client, settings, stream=memory.stream, session_id=session_id, usage=self.usage,
                                 embedder=embedder, feedback=memory.feedback, index=index, preferences=memory.preferences_file)
        if embedder is not None:
            self.clients.append(embedder)  # closed with the others at teardown
        self.preference = PreferenceUpdater(
            paths,
            preference_client,
            settings,
            memory,
            stream=memory.stream,
            session_id=session_id,
            usage=self.usage,
            index=index,
        )
        # The idle trigger (proposer/idle.py): its own proposer and embedder beside the timer's, so neither waits on the other.
        quick_embedder = EmbeddingClient(settings.embed, memory.embeddings) if settings.proposer_dedup else None
        self.quick = QuickProposer(paths, proposer_client, settings, stream=memory.stream, session_id=session_id, usage=self.usage,
                                   embedder=quick_embedder, feedback=memory.feedback, preferences=memory.preferences_file)
        if quick_embedder is not None:
            self.clients.append(quick_embedder)
        self.idle = IdleTrigger(cooldown_s=settings.proposer_idle_cooldown_s, away_s=settings.proposer_idle_away_s)
        self.idle.configure(settings.proposer_idle_trigger, settings.proposer_idle_s, monotonic())
        self.screen = ScreenWatch(paths.snapshots)
        self.fires: set[asyncio.Task[None]] = set()  # an idle round and its belief refresh, while they run
        self.runs = SessionRuns(paths, persist=self.persist, stream=memory.stream)
        self.chat = Chat(paths, chats or ChatStore(memory), persist=self.persist, stream=memory.stream)
        self.tasks: list[asyncio.Task[None]] = []
        # One lock per role: a long thinking proposer must not delay the next summary tick.
        self.summary_lock = asyncio.Lock()
        self.belief_lock = asyncio.Lock()
        self.proposer_lock = asyncio.Lock()
        self.next_summary_at: str | None = None
        self.next_belief_at: str | None = None
        self.next_proposal_at: str | None = None
        self.next_preference_at: str | None = None
        # Cleared while the session is paused: the three loops wait on it before a tick, and no run starts. The observer
        # is a separate process and reads the pause flag file instead. The time limit counts running time: see deadline().
        self.running = asyncio.Event()
        self.running.set()
        self.paused_at: str | None = None
        self.paused_s = 0.0  # how long the session has been paused in all, pauses that ended
        self.last_error: str | None = None
        self.browser_workspace: str | None = None  # the cmux agent's browser workspace, opened on its first run (actor/cmux/browser.py)
        self.agent_browser: AgentBrowser | None = None  # the claude and codex agents' browser, opened on the first such run (actor/standalone/browser.py)
        self.remote_view: RemoteView | None = None  # the remote desktop as the person watches it from the monitor (actor/standalone/remote_view.py)

    def deadline(self) -> str | None:
        """When the time limit falls: ``expires_at`` already moved past every pause that ended, plus the one that is open."""
        if self.expires_at is None or self.paused_at is None:
            return self.expires_at
        return shifted(self.expires_at, seconds_between(self.paused_at, timestamp()))

    def record(self) -> dict[str, Any]:
        return {"id": self.id, "folder": str(self.paths.root), "startedAt": self.started_at, "stoppedAt": self.stopped_at,
                "stopReason": self.stop_reason, "expiresAt": self.deadline(), "pausedAt": self.paused_at, "pausedS": round(self.paused_s),
                "settings": self.settings.wire(),
                "usage": self.usage.get("usage"), "runs": self.runs.ids, "chatRuns": [run_id for run_id in self.runs.ids if run_id in self.runs.chat_ids],
                "lastError": self.last_error}

    def persist(self) -> None:
        write_json(self.paths.session_json, self.record())


class LiveSessionManager:
    def __init__(self, *, data_root: Path | str, settings: LiveSettings,
                 client_factory: ClientFactory = LiveModelClient,
                 observer_factory: ObserverFactory = ObserverProcess.spawn) -> None:
        self.data_root = Path(data_root)
        self.defaults = settings
        self.memory = MemoryStore(self.data_root).create()  # shared by every session: the summary stream and the task list
        self.chats = ChatStore(self.memory)  # the conversations with the agent, across sessions
        self.index = HistoryIndex(self.memory.root / "search.sqlite", self.memory.stream)  # the search tool's view of the stream
        self.client_factory = client_factory
        self.observer_factory = observer_factory
        self.session: LiveSession | None = None
        self.packing: Packing | None = None  # the stored sessions being compressed, or the last ones (storage/pack.py)
        self.starting = False
        self.digests: set[asyncio.Task[None]] = set()  # feedback digests in flight; a decision returns before its digest lands
        self.preference_lock = asyncio.Lock()  # one preference tick at a time: the timer's, the final one at a stop

    # ----- lifecycle --------------------------------------------------------------

    async def start(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.session is not None or self.starting:
            raise RunnerCoreError("A live session is already active.", code="live_session_active", status_code=409,
                                  hint="Stop the active session before starting another one.")
        try:
            settings = self.defaults.with_overrides(overrides or {})
            settings.require_models()
            settings.require_desktops()
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        self.starting = True
        session_id = new_session_id()
        paths = session_paths(self.data_root, session_id)
        if paths.root.exists():
            session_id = f"{session_id}-{int(asyncio.get_running_loop().time() * 1000) % 1000:03d}"
            paths = session_paths(self.data_root, session_id)
        paths.create()
        session = LiveSession(session_id, paths, settings, self.client_factory(settings.summary),
                              self.client_factory(settings.belief), self.client_factory(settings.proposer), self.memory, self.index,
                              preference_client=self.client_factory(settings.preference), chats=self.chats)
        try:
            if settings.max_session_minutes > 0:
                session.expires_at = plus_seconds(settings.max_session_minutes * 60)
            session.persist()
            session.observer = await self.observer_factory(paths, settings, self.memory.stream.root)
            session.tasks = [
                asyncio.create_task(self._loop(session, "summary"), name=f"live-summary:{session_id}"),
                asyncio.create_task(self._loop(session, "belief"), name=f"live-belief:{session_id}"),
                asyncio.create_task(self._loop(session, "preference"), name=f"live-preference:{session_id}"),
                asyncio.create_task(self._loop(session, "proposer"), name=f"live-proposer:{session_id}"),
                asyncio.create_task(self._idle_loop(session), name=f"live-idle:{session_id}"),
            ]
            if session.expires_at is not None:
                session.tasks.append(asyncio.create_task(self._expire(session), name=f"live-expire:{session_id}"))
            self.session = session
        except BaseException:
            await self._teardown(session)
            raise
        finally:
            self.starting = False
        return self.status()

    async def stop(self, reason: str | None = None) -> dict[str, Any]:
        session = self.session
        if session is None or session.stop_reason is not None:  # nothing running, or already tearing down
            raise RunnerCoreError("No live session is active.", code="live_session_inactive", status_code=409,
                                  hint="Start a session first.")
        if not session.running.is_set():
            self._end_pause(session)  # a session stopped while paused: the pause counts, the flag goes, the final ticks run
        session.stop_reason = reason or "stopped by operator"
        try:
            await self._teardown(session, final_tick=True)  # ends with the last session.json write
        finally:
            self.session = None  # only now a stored session, so a rename cannot be overwritten by that write
        return {**self.status(), "stopped": session.record()}

    def pause(self) -> dict[str, Any]:
        """Hold everything: the observer's looks, the three loops, new runs, the time limit."""
        session = self._active()
        if not session.running.is_set():
            raise RunnerCoreError("The live session is already paused.", code="live_session_paused", status_code=409,
                                  hint="Resume it first.")
        session.running.clear()
        session.paused_at = timestamp()
        session.paths.pause_flag.touch()
        self.memory.stream.emit("session_paused", {}, ts=session.paused_at, session_id=session.id)
        session.persist()
        return self.status()

    def resume(self) -> dict[str, Any]:
        session = self._active()
        if session.running.is_set():
            raise RunnerCoreError("The live session is not paused.", code="live_session_running", status_code=409,
                                  hint="Pause it first.")
        self._end_pause(session)
        if session.expires_at is not None and seconds_between(timestamp(), session.expires_at) < RESUME_GRACE_S:
            session.expires_at = plus_seconds(RESUME_GRACE_S)  # paused just before the limit: not stopped the moment it resumes
        self.memory.stream.emit("session_resumed", {"pausedS": round(session.paused_s)}, ts=timestamp(), session_id=session.id)
        session.persist()
        return self.status()

    def _end_pause(self, session: LiveSession) -> None:
        """Count the pause that is open, move the deadline past it, drop the flag the observer reads, release the loops."""
        held = seconds_between(session.paused_at or timestamp(), timestamp())
        session.paused_s += held
        if session.expires_at is not None:
            session.expires_at = shifted(session.expires_at, held)
        session.paused_at = None
        session.paths.pause_flag.unlink(missing_ok=True)
        session.running.set()

    def require_running(self) -> None:
        """Refuse what a paused session must not do (a run, a manual tick); without a session there is nothing to refuse."""
        if self.session is not None and not self.session.running.is_set():
            raise RunnerCoreError("The live session is paused.", code="live_session_paused", status_code=409,
                                  hint="Resume the session first.")

    async def shutdown(self) -> None:
        if self.session is not None:
            try:
                await self.stop("backend shutdown")
            except Exception:
                logger.exception("Live session shutdown failed")

    async def _expire(self, session: LiveSession) -> None:
        """Stop at the time limit, counted over running time: a pause moves the deadline by as long as it lasted."""
        assert session.expires_at is not None  # created only for a session with a limit
        while True:
            await asyncio.sleep(seconds_between(timestamp(), session.expires_at))
            await session.running.wait()  # paused at the deadline: the resume moves it
            if seconds_between(timestamp(), session.expires_at) == 0:
                break  # the deadline stood
        if self.session is session:
            logger.info("Live session %s reached its time limit; stopping.", session.id)
            await self.stop(MAX_SESSION_REASON)

    async def _teardown(self, session: LiveSession, *, final_tick: bool = False) -> None:
        current = asyncio.current_task()
        others = [task for task in [*session.tasks, *session.fires] if task is not current]
        for task in others:
            task.cancel()
        for task in others:
            try:
                await task
            except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001 - loop errors are already recorded
                logger.debug("Live loop task ended: %r", error)
        if session.observer is not None:
            try:
                await session.observer.stop()
            except Exception as error:  # noqa: BLE001 - record and continue closing
                session.last_error = f"observer stop: {error}"
        if session.browser_workspace is not None:
            try:
                await close_workspace(session.browser_workspace)
            except Exception as error:  # noqa: BLE001 - record and continue closing
                session.last_error = f"browser workspace close: {error}"
        if session.agent_browser is not None:
            try:
                await session.agent_browser.close()
            except Exception as error:  # noqa: BLE001 - record and continue closing
                session.last_error = f"agent browser close: {error}"
        if session.remote_view is not None:
            try:
                await session.remote_view.release()
            except Exception as error:  # noqa: BLE001 - record and continue closing
                session.last_error = f"remote view close: {error}"
        if final_tick:  # the notes, the task list and the preferences end complete; no suggestion for a session nobody will act on
            try:
                await self._summary_tick(session)
                await self._belief_tick(session)
                await self._preference_tick(session)
            except Exception as error:  # noqa: BLE001
                session.last_error = f"final tick: {error}"
        session.stopped_at = timestamp()
        session.persist()
        await self.wait_for_digests()  # a digest in flight still uses this session's proposer client
        for client in session.clients:
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as error:  # noqa: BLE001 - closing a model client is best effort
                logger.debug("Live model client close failed: %r", error)

    # main event loop for continous input -> task
    async def _loop(self, session: LiveSession, kind: str) -> None:
        next_at, output, tick = {
            "summary": ("next_summary_at", "summaries", self._summary_tick),
            "belief": ("next_belief_at", "beliefs", self._belief_tick),
            "proposer": ("next_proposal_at", "proposals", self._proposer_tick),
            "preference": ("next_preference_at", "preferences", self._preference_tick),
        }[kind]
        while True:
            interval = getattr(session.settings, f"{kind}_interval_s")  # read each round: tune() changes the proposer's
            setattr(session, next_at, plus_seconds(interval))
            await asyncio.sleep(interval)
            await session.running.wait()  # paused: the tick waits for the resume, then catches up on what was seen before
            setattr(session, next_at, None)  # None while the tick runs: the console shows it as running
            try:
                await tick(session)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                session.last_error = f"{kind}: {type(error).__name__}: {error}"[:300]
                logger.exception("Live %s tick failed", kind)
                append_jsonl(getattr(session.paths, output), {"ts": timestamp(), "error": session.last_error})
                session.persist()

    async def _summary_tick(self, session: LiveSession, *, force: bool = False) -> Summary | None:
        async with session.summary_lock:
            record = await session.summarizer.tick(
                force=force,
                finalize=lambda summary: self.memory.append_summary(summary, session.id),
            )
            if record is not None:
                session.persist()
            return record

    async def _belief_tick(self, session: LiveSession, *, force: bool = False) -> BeliefUpdate | None:
        async with session.belief_lock:
            update = await session.belief.tick(force=force)
            if update is not None:
                session.persist()
            return update

    async def _proposer_tick(self, session: LiveSession, *, force: bool = False) -> ProposalSet | None:
        async with session.proposer_lock:
            proposal_set = await session.proposer.tick(force=force, finalize=lambda record: notify_proposals(record, session.settings))
            if proposal_set is not None:
                session.persist()
            return proposal_set

    # ----- the idle trigger: a quick proposal on a pause, beside the timer (proposer/idle.py, proposer/quick.py) ---------

    async def _idle_loop(self, session: LiveSession) -> None:
        """Twice a second, while the trigger is on: the time since the last input and the screen in front. A fire runs
        as its own task, so the watch goes on and sees whether the person moved on before the proposal was ready."""
        while True:
            await asyncio.sleep(IDLE_POLL_S)
            await session.running.wait()  # paused: nothing is watched
            if session.idle.state == OFF:
                continue
            idle = input_idle_s()
            if idle is None:
                continue  # not a Mac: there is no input clock to watch
            session.screen.update()
            if session.idle.step(monotonic(), idle, session.screen.context, self._idle_ready(session), acted=action_idle_s()):
                self._idle_task(session, self._idle_fire(session, idle))

    def _idle_task(self, session: LiveSession, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work, name=f"live-idle-round:{session.id}")
        session.fires.add(task)
        task.add_done_callback(session.fires.discard)

    def _idle_ready(self, session: LiveSession) -> bool:
        """Whether a pause may fire now: a readable app in front, looked at lately (the console in front writes no look,
        a blocked app is not read), no run of this session going (an agent at work is not the person stuck), the screen
        not locked."""
        latest = session.screen.latest
        recent = latest is not None and seconds_between(str(latest.get("ts") or ""), timestamp()) <= max(3 * session.settings.snapshot_interval_s, 30.0)
        return recent and session.screen.in_front() is not None and not session.runs.active and not screen_locked()

    async def _idle_fire(self, session: LiveSession, paused_s: float) -> None:
        """One idle round: a fresh look, then the quick proposal. The task list is refreshed beside it (the pending
        summary, then the belief update), not before it, so the proposal does not wait on the slowest step."""
        shown: str | None = None
        paused_at = shifted(timestamp(), -paused_s)  # when the pause began, before the look below takes its time
        try:
            current = await self._fresh_look(session)
            if current is None:
                return  # the console came to the front, or the app is not readable: nothing to propose on
            self._idle_task(session, self._idle_beliefs(session))
            record = await session.quick.propose(paused_s=paused_s, current=current, paused_at=paused_at,
                                                 finalize=lambda record: self._idle_finalize(session, record))
            session.persist()
            shown = record.ts if record.effect in ("notified", "proposed") else None
        except asyncio.CancelledError:
            raise
        except Exception as error:  # recorded like a loop's failure; the trigger waits for the next pause
            session.last_error = f"idle: {type(error).__name__}: {error}"[:300]
            logger.exception("Live idle round failed")
            append_jsonl(session.paths.proposals, {"ts": timestamp(), "error": session.last_error, "trigger": "idle"})
            session.persist()
        finally:
            session.idle.landed(shown)

    async def _fresh_look(self, session: LiveSession) -> dict[str, Any] | None:
        """Ask the observer for a look now and wait for it: the screen in front when it is readable, else None. No look in
        time means the console is in front (the observer records nothing of it)."""
        asked = timestamp()
        session.paths.look_flag.touch()
        deadline = monotonic() + LOOK_WAIT_S
        while monotonic() < deadline:
            await asyncio.sleep(0.1)
            session.screen.update()
            looked = str((session.screen.latest or {}).get("ts") or "")
            if looked >= asked:
                front = session.screen.in_front()
                frame_by = monotonic() + FRAME_WAIT_S  # the recorder takes this look's frame just after it: the round attaches it
                while front is not None and session.screen.frame != looked and monotonic() < frame_by:
                    await asyncio.sleep(0.1)
                    session.screen.update()
                return front
        session.paths.look_flag.unlink(missing_ok=True)
        return None

    async def _idle_beliefs(self, session: LiveSession) -> None:
        """The idle round's belief step: what the observer saw since the last summary summarized, then folded into the
        task list. Failures are the session's last error; the proposal does not depend on it."""
        try:
            await self._summary_tick(session)
            await self._belief_tick(session)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            session.last_error = f"idle belief: {type(error).__name__}: {error}"[:300]
            logger.exception("Live idle belief refresh failed")

    def _idle_finalize(self, session: LiveSession, record: ProposalSet) -> ProposalSet:
        """Notify as the timer's rounds do, unless the person went on (a key, a click, a scroll) before the proposal was
        ready: then it stays on the record, out of view (an autocomplete drops a suggestion the typing has overtaken). A
        mouse that only moved does not count: the person may just be reaching for it."""
        acted = action_idle_s()
        if acted is not None and session.idle.resumed(monotonic(), acted):
            return stale(record)
        return notify_proposals(record, session.settings)

    def set_idle_trigger(self, enabled: bool, idle_s: float) -> dict[str, Any]:
        """The console's switch: the running session's trigger at once, and the next sessions' default (settings.yaml)."""
        return self.tune({"proposerIdleTrigger": enabled, "proposerIdleS": idle_s})

    def tune(self, values: dict[str, Any]) -> dict[str, Any]:
        """A setting changed from the landing page (TUNABLE: the idle switch, the sliders): the running session at once,
        and the next sessions' default (settings.yaml)."""
        unknown = sorted(set(values) - set(TUNABLE))
        if unknown:
            raise RunnerCoreError(f"Not tunable while a session runs: {', '.join(unknown)}.", code="invalid_request", status_code=400)
        session = self.session if self.session is not None and self.session.stop_reason is None else None
        try:
            self.defaults.save(values)
            self.defaults = LiveSettings.load()
            if session is not None:
                session.settings = session.settings.with_overrides(values)
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        if session is not None:
            settings = session.settings
            session.proposer.settings = session.quick.settings = settings  # the duplicate filter's threshold
            session.idle.configure(settings.proposer_idle_trigger, settings.proposer_idle_s, monotonic())
            session.idle.cooldown_s = settings.proposer_idle_cooldown_s
            if "proposerIntervalS" in values:
                self._retime(session)
            session.persist()
        return self.status()

    def _retime(self, session: LiveSession) -> None:
        """The proposer's loop starts over on the new interval; while its tick runs, the loop reads the interval after it."""
        if session.next_proposal_at is None:
            return
        name = f"live-proposer:{session.id}"
        for index, task in enumerate(session.tasks):
            if task.get_name() == name:
                task.cancel()
                session.tasks[index] = asyncio.create_task(self._loop(session, "proposer"), name=name)

    # ----- queries used by the console -------------------------------------------------

    async def propose_now(self) -> dict[str, Any]:
        """Debug helper: summarize what is pending, then proposer it, regardless of the timers."""
        session = self._active()
        self.require_running()
        summary = await self._summary_tick(session, force=True)
        await self._belief_tick(session)  # only if the stream has worklog lines the belief updater has not read
        proposal_set = await self._proposer_tick(session, force=True)
        return {"summary": summary.wire() if summary else None, "proposals": proposal_set.wire() if proposal_set else None}

    def status(self) -> dict[str, Any]:
        session = self.session
        payload: dict[str, Any] = {"active": session is not None, "defaults": self.defaults.wire(), "rejectReasons": list(REJECT_REASONS),
                                   "limits": {"maxProposals": MAX_PROPOSALS_CAP}, "session": None,
                                   "tasks": [task.wire() for task in self.memory.tasks()],  # the belief, across sessions
                                   "trash": [task.wire() for task in self.memory.trash()],  # what can still be restored
                                   "taskFlags": self.memory.task_flags()}  # the operator's latest thumbs up / down per task and entry
        if session is None:
            return payload
        snapshots, _ = read_jsonl(session.paths.snapshots)
        if session.observer is not None and not session.observer.alive() and session.stop_reason is None and not session.last_error:
            # The recorder died on its own: its log holds the traceback; the last line is the reason.
            lines = [line for line in session.paths.observer_log.read_text(errors="replace").splitlines() if line.strip()] \
                if session.paths.observer_log.exists() else []
            session.last_error = f"observer exited: {lines[-1][:300] if lines else 'no output'}"
        latest = next((item for item in reversed(snapshots) if row_kind(item) in ("screen_changed", "screen_unchanged")), None)
        shot = next((item for item in reversed(snapshots) if row_kind(item) == "screenshot_taken"), None)
        proposals = [item for item in annotated_proposals(session.paths, 5) if "error" not in item]
        payload["session"] = {
            **session.record(),
            "observerAlive": bool(session.observer is not None and session.observer.alive()),
            "paused": not session.running.is_set(),
            "snapshotCount": sum(1 for item in snapshots if row_kind(item) == "screen_changed"),
            "tickCount": len(snapshots),
            "screenshotCount": sum(1 for item in snapshots if row_kind(item) == "screenshot_taken"),
            "latestSnapshot": {"ts": latest.get("ts"), "app": latest.get("app"), "window": latest.get("window"), "visible": latest.get("visible"),
                               "blocked": (latest.get("reason") or "app") if latest.get("excluded") else None} if latest else None,
            "latestScreenshot": {"ts": shot.get("ts"), "name": shot.get("path"), "app": shot.get("app"), "window": shot.get("window")} if shot else None,
            "permissionAsks": self.permission_asks(session, snapshots),
            "agentBrowser": session.agent_browser.wire() if session.agent_browser is not None else None,
            "summaryTicks": session.summarizer.ticks,
            "beliefTicks": session.belief.ticks,
            "proposerTicks": session.proposer.ticks,
            "latestProposals": proposals[-1] if proposals else None,
            "nextSummaryAt": session.next_summary_at,
            "nextBeliefAt": session.next_belief_at,
            "nextProposalAt": session.next_proposal_at,
            "idleTrigger": session.idle.wire(input_idle_s() if session.idle.state != OFF else None),
        }
        return payload

    def summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        return annotated_summaries(self._active().paths, limit)

    def proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        return annotated_proposals(self._active().paths, limit)

    def record_summary_feedback(self, summary_ts: str, comment: str, verdict: str = "wrong",
                                session_id: str | None = None) -> dict[str, Any]:
        paths = self._active().paths if session_id is None else stored_paths(self.data_root, session_id)
        record = record_summary_feedback(paths, summary_ts, comment, verdict)
        self.memory.stream.emit("summary_flagged", record, ts=record["ts"], session_id=paths.root.name)
        return record

    async def record_feedback(self, proposals_ts: str, decision: str, session_id: str | None = None,
                              choice: int | None = None) -> dict[str, Any]:
        """Feedback on the active session, or on a stored one when ``session_id`` is given (labeling past runs).

        The decision is written at once, to the session's feedback file and to memory, and returned; its context, what
        the proposer saw that round condensed by the proposer's model, is made in the background and written onto both
        rows when it lands (``digestError`` on the session row when it fails). The console shows which it is.
        """
        if session_id is None:
            session, paths = self._active(), self._active().paths
        else:
            session, paths = None, stored_paths(self.data_root, session_id)
        stored = {"id": new_id(), **feedback_record(paths, proposals_ts, decision, choice).wire()}
        append_jsonl(paths.feedback, stored)
        self.memory.append_decision(Decision.from_feedback(stored, session_id or paths.root.name))
        self.memory.stream.emit(
            "proposal_accepted" if decision == "accept" else "proposal_rejected",
            stored,
            ts=stored["ts"],
            session_id=session_id or paths.root.name,
            call_id=stored.get("callId") or "",
            id=stored["id"],
        )
        if session is not None:
            session.idle.answered(proposals_ts)  # an answer to an idle round's proposal: the next pause is idle_s again
        self._background(self._digest(session, paths, stored, session_id or paths.root.name))
        return stored

    def _background(self, work: Coroutine[Any, Any, None]) -> None:
        """A digest, run in the background and awaited at teardown (``wait_for_digests``)."""
        task = asyncio.create_task(work)
        self.digests.add(task)
        task.add_done_callback(self.digests.discard)

    async def _digest(self, session: LiveSession | None, paths: SessionPaths, stored: dict[str, Any], session_id: str) -> None:
        """The digest for one decision: the running session's proposer client, or one on the default proposer endpoint."""
        settings = session.settings if session is not None else self.defaults
        client = session.proposer.client if session is not None else self.client_factory(settings.proposer)
        try:
            reply = await feedback_digest(client, paths, stored["proposalsTs"], max_side=settings.screenshot_max_side)
            updates = {"context": reply.content.strip(), "model": reply.model, "usage": reply.usage}
            if session is not None:
                record_usage(session.usage, reply.model, reply.usage)
        except Exception as error:  # noqa: BLE001 - the decision stands; the row says its digest failed
            updates = {"digestError": f"{type(error).__name__}: {str(error)[:300]}"}
        finally:
            if session is None:
                await client.close()
        amend_jsonl(paths.feedback, lambda row: row.get("ts") == stored["ts"] and row.get("proposalsTs") == stored["proposalsTs"], updates)
        if "context" in updates:
            self.memory.amend_decision(stored["ts"], session_id, {"context": updates["context"]})
        self.memory.stream.emit(
            "decision_digested",
            {"proposalsTs": stored["proposalsTs"], "decisionTs": stored["ts"], **updates},
            ts=timestamp(),
            session_id=session_id,
            call_id=stored.get("callId") or "",
        )

    async def _preference_tick(self, session: LiveSession, *, force: bool = False) -> PreferenceUpdate | None:
        """The reactions since the cursor, any session's, when at least ``preference_min_feedback`` wait; the model may leave
        the file as it is. Fewer wait for the next tick, or the next session's: a reaction between sessions waits the same way."""
        async with self.preference_lock:
            update = await session.preference.tick(force=force)
            if update is not None:
                session.persist()
            return update

    async def wait_for_digests(self) -> None:
        """Until every digest in flight has landed (tests, and teardown before the clients close)."""
        if self.digests:
            await asyncio.gather(*self.digests, return_exceptions=True)

    def _flag_target(self, task_id: str, step: int | None) -> tuple[str, str, str]:
        """The title of a task on the visible list, the text of its entry ``step`` ("" for the task itself), and the
        call id of whichever of the two the flag is on."""
        task = next((item for item in self.memory.tasks() if item.id == task_id), None)
        if task is None:
            raise RunnerCoreError("Unknown task.", code="invalid_request", status_code=404)
        if step is not None and not 1 <= step <= len(task.steps):
            raise RunnerCoreError("No such entry.", code="invalid_request", status_code=400)
        entry = task.steps[step - 1] if step is not None else None
        return task.title, entry.text if entry else "", entry.call_id if entry else task.call_id

    def flag_task(self, task_id: str, step: int | None, verdict: str) -> dict[str, Any]:
        """The operator's thumbs up or down on a task or on one of its entries, as the list shows them: recorded in
        memory as feedback on the belief updater. One record per task or entry: a second click updates it (a changed
        verdict drops the reason). An entry's flag follows its text when its number shifts. Nothing on the list changes,
        and the flag outlives a later delete."""
        if verdict not in ("up", "down"):
            raise RunnerCoreError("verdict must be 'up' or 'down'.", code="invalid_request", status_code=400)
        title, text, call_id = self._flag_target(task_id, step)
        now = timestamp()
        current = self.memory.current_task_flag(task_id, step, text)
        if current is not None:
            changed = current.get("verdict") != verdict
            row = self.memory.update_task_flag(current["id"], {"ts": now, "step": step, "verdict": verdict, **({"reason": ""} if changed else {})})
        else:
            flag = TaskFlag(id=uuid4().hex[:12], ts=now, session_id=self.session.id if self.session is not None else "", task_id=task_id,
                            title=title, step=step, text=text, verdict=verdict, call_id=call_id)  # type: ignore[arg-type]
            self.memory.append_task_flag(flag)
            row = flag.wire()
        self.memory.stream.emit("task_flagged", row, ts=now, session_id=self._session_id(), call_id=row.get("callId") or "")
        return row

    def _session_id(self) -> str:
        """The running session's id for an event the operator causes, or "" between sessions."""
        return self.session.id if self.session is not None else ""

    def flag_task_reason(self, flag_id: str, reason: str) -> dict[str, Any]:
        """Why the thumbs down, given after the fact: written onto that flag."""
        reason = " ".join(reason.split())[:200]
        if not reason:
            raise RunnerCoreError("reason must not be empty.", code="invalid_request", status_code=400)
        if self.memory.update_task_flag(flag_id, {}).get("verdict") != "down":
            raise RunnerCoreError("That flag is not a thumbs down.", code="invalid_request", status_code=400)
        row = self.memory.update_task_flag(flag_id, {"reason": reason})
        self.memory.stream.emit("task_flagged", row, ts=timestamp(), session_id=self._session_id(), call_id=row.get("callId") or "")
        return row

    def unflag_task(self, flag_id: str) -> dict[str, Any]:
        """A flag taken back: the record is removed."""
        self.memory.remove_task_flag(flag_id)
        self.memory.stream.emit("task_unflagged", {"id": flag_id}, ts=timestamp(), session_id=self._session_id())
        return {"id": flag_id, "deleted": True}

    def proposal_sent(self, proposals_ts: str, text: str) -> dict[str, Any] | None:
        """An accepted proposal sent to the chat as the next message: with its text changed first, its acceptance records what
        was sent (``storage/feedback.record_sent``); sent as proposed, nothing is added."""
        return record_sent(self._active().paths, proposals_ts, text)

    def record_reject_reason(self, proposals_ts: str, reason: str, typed: bool, session_id: str | None = None) -> dict[str, Any]:
        """Why the latest rejection of a proposal set was made, given after the fact, and whether the person typed it
        or clicked a preset: written onto the decision and its memory note, so the proposer's feedback source carries
        it."""
        paths = self._active().paths if session_id is None else stored_paths(self.data_root, session_id)
        row = reject_reason(paths, proposals_ts, reason, typed)
        self.memory.amend_decision(row["ts"], session_id or paths.root.name, {"rejectReason": row["rejectReason"], "reasonTyped": typed})
        self.memory.stream.emit(
            "reject_reason_given",
            {"proposalsTs": proposals_ts, "decisionTs": row["ts"], "reason": row["rejectReason"], "typed": typed},
            ts=timestamp(),
            session_id=session_id or paths.root.name,
            call_id=row.get("callId") or "",
        )
        return row

    def permission_asks(self, session: LiveSession, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The permission questions with no answer yet, newest first: the agent's asks (its run waits on them, so they come first,
        with the level and why) and the apps the observer looked at under ask, minus those a rule now covers (settings.yaml's or
        the session's own). Derived, so an answer, a hand edit or the session's end empty it."""
        asked: dict[tuple[str, str], str] = {}
        for item in snapshots:
            if item.get("excluded") and item.get("reason") == Reason.ASK:
                asked[(str(item.get("app") or ""), str(item.get("bundle") or ""))] = str(item.get("ts") or "")
        if not asked and not session.paths.asks.exists():
            return []
        permissions = LiveSettings.load().permissions_with(session.paths.settings)
        looked = [{"app": app, "bundle": bundle, "ts": ts} for (app, bundle), ts in sorted(asked.items(), key=lambda item: item[1], reverse=True)
                  if permissions.decide(app, bundle)[1] == Reason.ASK]
        return pending_asks(session.paths.asks, permissions) + looked

    def answer_permission(self, app: str, level: str, scope: str = "") -> dict[str, Any]:
        """An answer to a permission question: block, look or use become the app's rule in settings.yaml, or with the scope
        session in this session's own settings.yaml, gone with the session, or with the scope once or task a grant for one step or the rest of the asking run, a row in its asks file;
        deny ends the agent's ask with no rule. The observer and the agent follow on their next look or call."""
        session = self._active()
        app = app.strip()
        if not app or level not in ("block", "look", "use", "deny") or scope not in ("", "session", "once", "task"):
            raise RunnerCoreError("An app and one of block, look, use or deny are needed; the scope session keeps the answer to this session, once to one step, task to the asking run.",
                                  code="invalid_request", status_code=400)
        if level == "deny" or scope in ("once", "task"):
            answer_ask(session.paths.asks, app, None if level == "deny" else level, scope)
        elif scope == "session":
            write_session_rules(session.paths.settings, {**{name: str(held) for name, held in session_rules(session.paths.settings).items()}, app: level})
        else:
            current = LiveSettings.load()
            current.save({"permissionsApps": {**{name: str(held) for name, held in current.permissions_apps.items()}, app: level}})
        return {"ok": True}

    def blocked_glimpse(self) -> bytes:
        """The frontmost window, blurred past reading, as a JPEG for the monitor while the latest look is blocked: so the person
        sees which window is not being read. Taken now, kept nowhere; a 409 at any other time."""
        from io import BytesIO

        from .observer.sources import blurred_glimpse

        snapshots, _ = read_jsonl(self._active().paths.snapshots)
        latest = next((item for item in reversed(snapshots) if row_kind(item) in ("screen_changed", "screen_unchanged")), None)
        if not (latest and latest.get("excluded")):
            raise RunnerCoreError("The latest look is not blocked", code="live_not_blocked", status_code=409)
        image = blurred_glimpse(int(latest.get("pid") or 0))
        if image is None:
            raise RunnerCoreError("No window to show", code="artifact_not_found", status_code=404)
        out = BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=60)
        return out.getvalue()

    def screenshot_png(self, name: str, session_id: str | None = None) -> bytes:
        """A stored screenshot: its file, or the picture out of a compressed session's pack (storage/pack.py)."""
        paths = self._active().paths if session_id is None else stored_paths(self.data_root, session_id)
        directory = paths.screenshots.resolve()
        path = (directory / Path(name).name).resolve()
        png = path.read_bytes() if path.is_relative_to(directory) and path.is_file() else screenshot(paths, Path(name).name)
        if png is None:
            raise RunnerCoreError("Screenshot not found", code="artifact_not_found", status_code=404)
        return png

    # ----- hooks used by the runner (the actor boundary) ---------------------------------------

    def actor_yield_s(self) -> float:
        """The exec_py yield time a run started now should use: the running session's setting, else the defaults."""
        return (self.session.settings if self.session is not None else self.defaults).actor_yield_s

    def desktop_source(self) -> tuple[str, ...]:
        """The desktops a run started now gets: the running session's setting, else the defaults."""
        return (self.session.settings if self.session is not None else self.defaults).actor_desktop_source

    def desktop_remote(self) -> tuple[str, str, str] | None:
        """The remote desktop a run started now connects to, (provider, url, token); None when the source is local only."""
        settings = self.session.settings if self.session is not None else self.defaults
        if "remote" not in settings.actor_desktop_source:
            return None
        try:
            settings.require_desktops()
        except ValueError as error:
            raise RunnerCoreError(str(error), code="invalid_request", status_code=400) from error
        return settings.remote_desktop_provider or "", settings.remote_desktop_url or "", settings.remote_desktop_token or ""

    def actor_agents(self) -> tuple[str, ...]:
        """The agents a run started now can use: the running session's setting, else the defaults."""
        return (self.session.settings if self.session is not None else self.defaults).actor_agents

    def cmux_settings(self) -> dict[str, str]:
        """What a cmux run started now carries on its run detail: the permission mode, and the Claude model and folder when set."""
        settings = self.session.settings if self.session is not None else self.defaults
        fields = {"actorCmuxModel": settings.actor_cmux_model, "actorCmuxPermissionMode": settings.actor_cmux_permission_mode, "actorCmuxCwd": settings.actor_cmux_cwd}
        return {key: value for key, value in fields.items() if value}

    def claude_settings(self) -> dict[str, str]:
        """What a claude run started now carries on its run detail: the permission mode, and the Claude model and folder when set."""
        settings = self.session.settings if self.session is not None else self.defaults
        fields = {"actorClaudeModel": settings.actor_claude_model, "actorClaudePermissionMode": settings.actor_claude_permission_mode,
                  "actorClaudeCwd": settings.actor_claude_cwd}
        return {key: value for key, value in fields.items() if value}

    def codex_settings(self) -> dict[str, str]:
        """What a codex run started now carries on its run detail: the sandbox, and the model and folder when set."""
        settings = self.session.settings if self.session is not None else self.defaults
        fields = {"actorCodexModel": settings.actor_codex_model, "actorCodexSandbox": settings.actor_codex_sandbox, "actorCodexCwd": settings.actor_codex_cwd}
        return {key: value for key, value in fields.items() if value}

    async def agent_browser_endpoint(self) -> str:
        """The session's agent browser, opened on the first claude or codex run and closed when the session stops: its CDP
        address. Its profile is one for every session, so logins are kept."""
        session = self._active()
        if session.agent_browser is None:
            browser = AgentBrowser(self.data_root / "agent-browser")
            await browser.start()
            session.agent_browser = browser
        return session.agent_browser.endpoint

    def remote_view(self) -> RemoteView:
        """The remote desktop as the monitor shows it, for a session whose desktops include it."""
        session = self._active()
        remote = self.desktop_remote()
        if remote is None:
            raise RunnerCoreError("This session has no remote desktop.", code="remote_unavailable", status_code=409,
                                  hint="Set the desktops to local,remote with the remote desktop's provider, URL and token.")
        if session.remote_view is None:
            session.remote_view = RemoteView(*remote)
        return session.remote_view

    def agent_browser(self) -> AgentBrowser:
        """The agent browser, for the monitor: an error until a claude or codex run has opened it."""
        session = self._active()
        if session.agent_browser is None:
            raise RunnerCoreError("The agent browser is not open.", code="browser_closed", status_code=409,
                                  hint="It opens with the first Claude Code run of the session.")
        return session.agent_browser

    async def cmux_browser_workspace(self) -> str:
        """The session's agent browser workspace in cmux: found or opened on the first cmux run, closed when the session stops."""
        session = self._active()
        if session.browser_workspace is None:
            session.browser_workspace = await browser_workspace()
        return session.browser_workspace

    def actor_inputs(self) -> ActorInputs | None:
        """What a task run started now is shown (its worklog and proposal), or None when no session is active."""
        return self.session.runs.actor_inputs(self.session.settings.actor_context_summaries, self.memory.preferences()) if self.session is not None else None

    def run_directory(self, run_id: str) -> Path | None:
        """Where the runner should keep a run started from the active session; None without a session."""
        return self.session.runs.directory(run_id) if self.session is not None else None

    def session_folder(self) -> Path | None:
        """The active session's folder, which a run's worker is told: the permissions' answers and the agent's asks live there."""
        return self.session.paths.root if self.session is not None else None

    def record_run(self, run_id: str, live_context: str | None, prompt: str = "", chat: dict[str, Any] | None = None,
                   proposal: dict[str, Any] | None = None) -> None:
        """A run started from the session; with ``chat``, the composed turn (``Chat.compose``) it is; with ``proposal``, the
        accepted proposal it carries out."""
        if self.session is not None:
            self.session.runs.started(run_id, live_context, prompt, chat=bool(chat), proposal=proposal)
            if chat:
                self.session.chat.started(run_id, chat)

    def record_run_finished(self, run_id: str, status: str, chat: dict[str, Any] | None = None) -> None:
        """A run ended; ``chat`` is the run detail's chat block when the run was a turn (its reply, what to resume)."""
        if self.session is not None:
            self.session.runs.finished(run_id, status)
            self.session.chat.finished(run_id, status, chat)

    # ----- the chat with the agent ------------------------------------------------------------------------------

    def chat(self, chat_id: str | None = None) -> dict[str, Any]:
        """The chat as the console shows it: the running session's; with none, a conversation to read (nothing current)."""
        if self.session is None:
            return read_only(self.chats, chat_id, self.chats.conversations())
        return self.session.chat.wire(chat_id)

    def stored_chat(self, session_id: str, chat_id: str | None = None) -> dict[str, Any]:
        """A stored session's conversations, read-only (the running session's through ``chat``)."""
        if self.session is not None and self.session.id == session_id:
            return self.session.chat.wire(chat_id)
        return stored_chat(self.chats, session_id, chat_id)

    def chats_list(self) -> dict[str, Any]:
        """Every conversation, latest first, the one the running session is on, and the names of the sessions they were in."""
        conversations = self.chats.conversations()
        return {
            "conversations": conversations,
            "current": (self.session.chat.id or None) if self.session is not None else None,
            "sessions": {sid: (read_json(self.data_root / "sessions" / sid / "session.json") or {}).get("name") for sid in sorted({sid for item in conversations for sid in item["sessions"]})},
        }

    def mark_chat(self, chat_id: str, **given: Any) -> dict[str, Any]:
        """A name for a conversation on the sidebar (in place of its first message), or its pin."""
        self.chats.mark(chat_id, **given)
        return self.chats_list()

    def chat_compose(self, text: str, agent: str, selected_tasks: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """The next chat turn, ready to start as a run (``chat`` on the start request)."""
        session = self._active()
        if agent not in session.settings.actor_agents:
            raise RunnerCoreError(f"This session did not set up the {agent} agent.", code="agent_unavailable", status_code=400,
                                  hint="Choose it under Actor in the session configuration, or actor.agents in settings.yaml.")
        return session.chat.compose(text, agent, session.runs.actor_inputs(session.settings.actor_context_summaries, self.memory.preferences()), selected_tasks)

    def chat_reset(self) -> dict[str, Any]:
        session = self._active()
        session.chat.reset()
        return session.chat.wire()

    def chat_switch(self, chat_id: str) -> dict[str, Any]:
        """Continue an earlier conversation: it is the one the next message goes to."""
        session = self._active()
        session.chat.switch(chat_id)
        return session.chat.wire()

    # ----- stored sessions (history view) ------------------------------------------------

    def session_detail(self, session_id: str, *, limit: int = 500) -> dict[str, Any]:
        """A stored session's record plus its summaries and annotated proposals, for the read-only history view."""
        paths = stored_paths(self.data_root, session_id)
        record = read_json(paths.session_json)
        if self.session is not None and self.session.id == session_id:
            record = self.session.record()
        snapshots, _ = read_jsonl(paths.snapshots)
        beliefs, _ = read_jsonl(paths.beliefs)
        return {
            "tasks": [task.wire() for task in tasks_left(beliefs, session_id)],  # the list as this session's last belief update left it
            "session": {
                **normalize_record(record if isinstance(record, dict) else {}, self.defaults.wire()),
                "snapshotCount": sum(1 for item in snapshots if row_kind(item) == "screen_changed"),
                "screenshotCount": sum(1 for item in snapshots if row_kind(item) == "screenshot_taken"),
                "active": self.session is not None and self.session.id == session_id,
            },
            "summaries": annotated_summaries(paths, limit),
            "proposals": annotated_proposals(paths, limit),
        }

    def list_sessions(self, limit: int = 20, offset: int = 0, named: bool | None = None) -> list[dict[str, Any]]:
        return previous_sessions(self.data_root, limit, self.defaults.wire(), offset, named)

    def usage_report(self, days: float) -> dict[str, Any]:
        """The cost panel: tokens and cost per model and module over every stored session, the last ``days`` days, and
        the running session (None when there is none). Read from the session files on each call."""
        root = self.data_root / "sessions"
        stored = [SessionPaths(folder) for folder in sorted(root.iterdir()) if (folder / "session.json").is_file()] if root.is_dir() else []
        since = plus_seconds(-days * 86400)
        return {"all": usage_rows(stored), "recent": usage_rows(stored, since=since), "byDay": usage_by_day(stored, since=since), "days": days,
                "session": usage_rows([self.session.paths]) if self.session is not None else None}

    def disk_report(self) -> dict[str, Any]:
        """The disk panel: the data folder's bytes by session and kind, what its volume has free, and how far along the
        sessions being compressed are (``packing``, None before any)."""
        return {**disk_report(self.data_root), "packing": self.packing.wire() if self.packing is not None else None}

    def pack_sessions(self, session_ids: list[str], *, restoring: bool = False) -> dict[str, Any]:
        """Start packing stored sessions' screenshots and model-call logs in place, or writing compressed ones back as
        they were, a few at a time (storage/pack.py); the disk report, whose ``packing`` then says how far along."""
        if self.packing is not None and self.packing.wire()["running"]:
            raise RunnerCoreError("Sessions are being compressed or restored already.", code="invalid_request", status_code=409)
        chosen = [self._past(session_id) for session_id in session_ids]
        for paths in chosen:
            if compressed(paths) != restoring:
                raise RunnerCoreError(f"{paths.root.name} is {'not compressed' if restoring else 'compressed already'}.", code="invalid_request", status_code=409)
        self.packing = Packing(chosen, restoring=restoring)
        return self.disk_report()

    def stored_usage(self, session_id: str) -> list[dict[str, Any]]:
        """The cost rows of one stored session."""
        return usage_rows([stored_paths(self.data_root, session_id)])

    def screenshots(self, session_id: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        """The screenshots the observer stored, for the running session or a stored one."""
        return screenshot_rows(self._active().paths if session_id is None else stored_paths(self.data_root, session_id), limit)

    def task_changes(self, session_id: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """The belief updates that changed the task list, for the running session or a stored one."""
        return task_changes(self._active().paths if session_id is None else stored_paths(self.data_root, session_id), limit)

    def _past(self, session_id: str) -> SessionPaths:
        """A stored session's folder; the running session cannot be renamed or deleted."""
        if self.session is not None and self.session.id == session_id:
            raise RunnerCoreError("Stop the session first.", code="live_session_active", status_code=409)
        return stored_paths(self.data_root, session_id)

    def rename_session(self, session_id: str, name: str | None) -> dict[str, Any]:
        """A display name in the session's session.json; the folder keeps its timestamp id."""
        paths = self._past(session_id)
        record = {**(read_json(paths.session_json) or {}), "name": name}
        write_json(paths.session_json, record)
        return normalize_record(record, self.defaults.wire())

    def delete_session(self, session_id: str) -> dict[str, Any]:
        shutil.rmtree(self._past(session_id).root)
        return {"id": session_id, "deleted": True}

    def edit_tasks(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Operator corrections to the belief (tick a step, finish a task): the same ops as the model's, applied
        under the same rules, logged with sessionId "operator"."""
        parsed = [op for op in (normalize_op({**item, "task_id": item.get("taskId", item.get("task_id"))})
                                for item in ops if isinstance(item, dict)) if op is not None]
        if not parsed:
            raise RunnerCoreError("No valid task operations.", code="invalid_request", status_code=400)
        settings = self.session.settings if self.session is not None else self.defaults
        now, call_id = timestamp(), uuid4().hex[:12]
        tasks = self.memory.apply(parsed, now=now, session_id="operator", call_id=call_id, cursor=None,
                                  stale_days=settings.belief_stale_days, done_step_days=settings.belief_done_step_days,
                                  trash_days=settings.belief_trash_days)
        self.memory.stream.emit(
            "tasks_edited",
            {"ops": [op.wire() for op in parsed], "tasks": [task.wire() for task in tasks]},
            ts=now,
            session_id=self._session_id(),
            call_id=call_id,
        )
        return {"tasks": [task.wire() for task in tasks], "trash": [task.wire() for task in self.memory.trash()]}

    @staticmethod
    def previous_sessions(data_root: Path | str, limit: int = 20, defaults: dict[str, Any] | None = None, offset: int = 0,
                          named: bool | None = None) -> list[dict[str, Any]]:
        return previous_sessions(data_root, limit, defaults, offset, named)

    def _active(self) -> LiveSession:
        if self.session is None:
            raise RunnerCoreError("No live session is active.", code="live_session_inactive", status_code=409,
                                  hint="Start a session first.")
        return self.session
