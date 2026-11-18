"""The session's side of its task runs.

A run is executed by ``RunnerManager`` in ``app/runner.py`` → ``execute_scenario`` in ``app/scenario_runtime.py`` → for a
run from the live page, ``run.py`` in this package. This module is what the session keeps about its runs: what a run is
shown (its worklog and latest proposal, as ``ActorInputs``), where its artifacts go (inside the session folder), and
what the session learns back (which tasks are in progress, so the proposer does not propose them again).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..storage.paths import SessionPaths
from ..storage.records import RunMarker, as_proposal_sets, as_summaries, new_id
from ..storage.stream import Stream
from ..utils.jsonl import append_jsonl, tail_jsonl
from ..utils.time import timestamp
from .prompts import ActorInputs


class SessionRuns:
    """The task runs of one session: their ids, which are still in progress, and the run log on disk."""

    def __init__(self, paths: SessionPaths, persist: Callable[[], None], *, stream: Stream) -> None:
        self.paths = paths
        self.stream = stream
        self._persist = persist  # rewrites session.json, which lists the run ids
        self.ids: list[str] = []
        self.chat_ids: set[str] = set()  # the runs that are chat turns: listed with the chat, not with the runs
        self.active: dict[str, str] = {}  # run id → prompt, while the run is in progress

    def directory(self, run_id: str) -> Path:
        """Where the runner keeps a run started from this session."""
        return self.paths.runs_dir / run_id

    def actor_inputs(self, summaries: int, preferences: str = "") -> ActorInputs:
        """What a run started now is shown: the session's last ``summaries`` worklog lines, the latest proposal, and the
        person's standing ``preferences``."""
        rounds = as_proposal_sets(tail_jsonl(self.paths.proposals, 20))
        return {
            "worklog": as_summaries(tail_jsonl(self.paths.summaries, summaries)),
            "proposal": next((item for item in reversed(rounds) if item.action in ("propose", "do")), None),
            "preferences": preferences,
        }

    def started(self, run_id: str, live_context: str | None, prompt: str = "", chat: bool = False,
                proposal: dict[str, Any] | None = None) -> None:
        """A run started with live context. The observer keeps recording: the person keeps working meanwhile."""
        self.ids.append(run_id)
        if chat:
            self.chat_ids.add(run_id)
        self.active[run_id] = prompt
        marker = {"id": new_id(), **RunMarker(ts=timestamp(), run_id=run_id, event="started", prompt=prompt, live_context=live_context, chat=chat or None,
                                              proposal=proposal).wire()}
        append_jsonl(self.paths.runs, marker)
        self.stream.emit("run_started", marker, ts=marker["ts"], session_id=self.paths.root.name, id=marker["id"])
        self._persist()

    def finished(self, run_id: str, status: str) -> None:
        if run_id not in self.active:
            return
        self.active.pop(run_id)
        marker = {"id": new_id(), **RunMarker(ts=timestamp(), run_id=run_id, event="finished", status=status).wire()}
        append_jsonl(self.paths.runs, marker)
        self.stream.emit("run_finished", marker, ts=marker["ts"], session_id=self.paths.root.name, id=marker["id"])
        self._persist()
