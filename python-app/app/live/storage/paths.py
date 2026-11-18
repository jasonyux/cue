"""Session folder layout under data/sessions/<timestamp>/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def new_session_id() -> str:
    """Local-time folder name so sessions sort chronologically."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True)
class SessionPaths:
    root: Path

    @property
    def session_json(self) -> Path:
        return self.root / "session.json"

    @property
    def settings(self) -> Path:
        """This session's own settings: the permission answers given for the session alone (permissions.apps), gone with it."""
        return self.root / "settings.yaml"

    @property
    def asks(self) -> Path:
        """The agent's asks for more than the permissions allow, and the denials among the answers (live/permissions.py)."""
        return self.root / "asks.jsonl"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots.jsonl"

    @property
    def screenshots(self) -> Path:
        return self.root / "screenshots"

    @property
    def summaries(self) -> Path:
        return self.root / "summaries.jsonl"

    @property
    def proposals(self) -> Path:
        return self.root / "proposals.jsonl"

    @property
    def beliefs(self) -> Path:
        """The task list after each belief update in this session, with the ops that produced it."""
        return self.root / "beliefs.jsonl"

    @property
    def preferences(self) -> Path:
        """Each preference update made while this session ran, with the file before and after (preference/update.py)."""
        return self.root / "preferences.jsonl"

    @property
    def model_calls(self) -> Path:
        return self.root / "live_model_calls.jsonl"

    @property
    def runs(self) -> Path:
        return self.root / "runs.jsonl"

    @property
    def runs_dir(self) -> Path:
        """Task runs started from this session keep their artifacts here (runs/<run id>/), not under data/runs."""
        return self.root / "runs"

    @property
    def chat(self) -> Path:
        """The chat with the agent, one row per message (live/actor/chat.py)."""
        return self.root / "chat.jsonl"

    @property
    def chat_json(self) -> Path:
        """The chat's state: its agent, turn count, worklog cursor, and the agent conversation to resume."""
        return self.root / "chat.json"

    @property
    def feedback(self) -> Path:
        return self.root / "feedback.jsonl"

    @property
    def summary_feedback(self) -> Path:
        return self.root / "summary_feedback.jsonl"

    @property
    def pause_flag(self) -> Path:
        return self.root / "observer.paused"

    @property
    def look_flag(self) -> Path:
        """Asks the observer for a look now (the idle trigger, before it proposes): taken within a nap, then removed."""
        return self.root / "observer.look"

    @property
    def observer_log(self) -> Path:
        return self.root / "observer.log"

    def create(self) -> SessionPaths:
        self.screenshots.mkdir(parents=True, exist_ok=True)
        for path in (self.snapshots, self.summaries, self.beliefs, self.proposals, self.preferences, self.model_calls, self.runs, self.feedback,
                     self.summary_feedback):
            path.touch(exist_ok=True)
        return self


def session_paths(data_root: Path | str, session_id: str) -> SessionPaths:
    return SessionPaths(Path(data_root) / "sessions" / session_id)
