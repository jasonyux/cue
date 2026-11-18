"""The runner's camelCase HTTP and version-3 replay contracts.

Optional properties are omitted on the wire. Start requests reject unknown fields.
"""
import re
from datetime import datetime
from types import UnionType
from typing import Annotated, Any, Literal, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]
TurnBudget = Annotated[int, Field(gt=0, le=300)]
LabId = Literal["kanban", "paint", "booking", "desktop"]
BrowserMode = Literal["headless", "headful"]
RunStatus = Literal["running", "completed", "failed", "cancelled"]
EventType = Literal[
    "run_started", "workspace_prepared", "lab_started", "browser_session_started", "browser_navigated",
    "function_call_requested", "function_call_completed", "tool_progress", "screenshot_captured", "run_progress",
    "run_completed", "run_failed", "run_cancelled",
]


def is_integer_annotation(annotation: Any) -> bool:
    if annotation is int:
        return True
    if get_origin(annotation) is Annotated:
        return is_integer_annotation(get_args(annotation)[0])
    return get_origin(annotation) in (Union, UnionType) and any(is_integer_annotation(part) for part in get_args(annotation))


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        if isinstance(value, dict):
            known = set(cls.model_fields) | {field.alias for field in cls.model_fields.values()}
            if any(item is None for key, item in value.items() if key in known):
                raise ValueError("Optional properties must be omitted, not null.")
            value = dict(value)
            for name, field in cls.model_fields.items():
                for key in (name, field.alias):
                    item = value.get(key)
                    if isinstance(item, float) and item.is_integer() and is_integer_annotation(field.annotation):
                        value[key] = int(item)
        return value

    def wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def valid_timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z", value):
        raise ValueError("Timestamp must be a UTC ISO date ending in Z.")
    datetime.fromisoformat(value[:-1] + "+00:00")
    return value


class ScenarioManifest(WireModel):
    id: Text
    lab_id: LabId
    category: Literal["productivity", "creativity", "commerce"]
    title: Text
    description: Text
    default_prompt: Text
    workspace_template_path: Text
    tags: Annotated[list[Text], Field(min_length=1)]


class RunSummary(WireModel):
    step_count: Count
    screenshot_count: Count
    notes: list[str]


class ModelUsage(WireModel):
    """Token counts accumulated over the Responses API calls made to one model during a run."""
    model: Text
    calls: Count
    input_tokens: Count
    cached_input_tokens: Count
    cache_write_tokens: Count
    output_tokens: Count
    reasoning_tokens: Count
    total_tokens: Count
    cost_usd: float | None = None


class RunUsage(WireModel):
    calls: Count
    input_tokens: Count
    cached_input_tokens: Count
    cache_write_tokens: Count
    output_tokens: Count
    reasoning_tokens: Count
    total_tokens: Count
    cost_usd: float | None = None
    by_model: list[ModelUsage]


class ProposalRef(WireModel):
    """The proposal a run carries out: the round it was in and the choice taken, so the run can be found from the proposal."""

    proposals_ts: Text
    choice: Annotated[int, Field(ge=0)]


class RunRecord(WireModel):
    id: Text
    scenario_id: Text
    lab_id: LabId
    browser_mode: BrowserMode
    model: Text
    max_response_turns: TurnBudget
    prompt: Text
    status: RunStatus
    started_at: str
    completed_at: str | None = None
    duration_ms: Count | None = None
    summary: RunSummary | None = None
    usage: RunUsage | None = None
    proposal: ProposalRef | None = None  # the accepted proposal the run carries out, when it came from one

    _dates = field_validator("started_at", "completed_at")(valid_timestamp)


class RunEvent(WireModel):
    id: Text
    run_id: Text
    sequence: Count
    type: EventType
    level: Literal["ok", "pending", "warn", "error"]
    message: Text
    detail: str | None = None
    created_at: str

    _date = field_validator("created_at")(valid_timestamp)


class BrowserViewport(WireModel):
    height: Positive
    width: Positive


class BrowserScreenshotArtifact(WireModel):
    source: Literal["browser_preview", "code_tool"] | None = None
    placeholder: bool | None = None  # a stand-in frame written when the desktop worker had no window to capture
    image_width: Positive | None = None
    image_height: Positive | None = None
    captured_at: str
    id: Text
    label: Text
    mime_type: Literal["image/png"]
    page_title: Text | None = None
    page_url: Text
    path: Text
    url: Text

    _date = field_validator("captured_at")(valid_timestamp)


class BrowserState(WireModel):
    current_url: Text
    mode: BrowserMode
    page_title: Text | None = None
    screenshots: list[BrowserScreenshotArtifact]
    target_label: Text
    viewport: BrowserViewport


class ChatTurn(WireModel):
    """A run that is one turn of the live session's chat: the person's message, what was observed since the previous
    turn (prepended to the message as the prompt), and the agent conversation to continue."""

    turn: Annotated[int, Field(gt=0)]
    agent: Literal["desktop", "cmux", "claude", "codex"]
    text: Text                      # the message as typed
    context: Text                   # the observed context prepended to it; the run's liveContext
    attached: int = 0               # how many worklog lines the context carries
    worklog_ts: str = ""            # the newest of them: where the next turn's context starts; "" while there is none yet
    resume: Text | None = None      # cmux, claude: Claude Code's session id to resume, codex: its thread, from the previous turn
    resume_totals: dict[str, Any] | None = None  # cmux, claude: that session's usage per model as the previous turn ended, as Claude reported it
    chat_id: Text | None = None     # the conversation (its folder under data/memory/chats, live/actor/chat.py); compose allots a new one
    cwd: Text | None = None         # that conversation's workspace, where claude and codex work the turn unless the settings name a folder


class StartRunRequest(WireModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False, validate_by_name=False)
    scenario_id: Text
    browser_mode: BrowserMode | None = None
    max_response_turns: TurnBudget | None = None
    prompt: Text
    model: Text | None = None
    live_context: bool | None = None  # attach the active live session's digest to the run instructions
    agent: Literal["desktop", "cmux", "claude", "codex"] | None = None  # with live_context: which of the session's agents runs it (desktop when omitted)
    chat: ChatTurn | None = None  # with live_context: this run is a turn of the session's chat; the prompt then carries the context
    proposal: ProposalRef | None = None  # with live_context: the accepted proposal this run carries out


class ChatRun(WireModel):
    """What a run that is a chat turn records: the turn number, the agent conversation it resumed and the one it
    leaves behind (cmux, claude: Claude Code session ids; codex: its thread id), and the agent's final reply as the chat shows it.
    For cmux and claude, the session's usage as the previous turn left it and as this one leaves it: the run's own usage is the difference."""

    turn: int
    resume: Text | None = None
    resume_totals: dict[str, Any] | None = None
    claude_session_id: Text | None = None
    claude_totals: dict[str, Any] | None = None
    codex_thread_id: Text | None = None
    reply: Text | None = None


class RunDetail(WireModel):
    run: RunRecord
    scenario: ScenarioManifest
    workspace_path: Text
    event_stream_url: Text
    replay_url: Text
    browser: BrowserState | None = None
    live_context: Text | None = None
    actor_yield_s: float | None = None  # set with live_context: the live actor's exec_py yield time for this run
    actor_desktop_source: list[Text] | None = None  # set with live_context: the desktops this run had, local and/or remote
    actor_session_folder: Text | None = None  # set with live_context for the agents with desktops: the session's folder, for the worker's permissions and asks
    desktop_remote_provider: Text | None = None  # set with live_context when the source includes remote: exec_py's remote target
    desktop_remote_url: Text | None = None
    actor_agent: Text | None = None  # set with live_context: the agent that ran it: desktop, cmux, claude or codex
    actor_cmux_permission_mode: Text | None = None  # set for a cmux run: claude's --permission-mode
    actor_cmux_model: Text | None = None  # and, when the session named them, its --model
    actor_cmux_cwd: Text | None = None  # and the folder Claude Code started in
    actor_cmux_browser_workspace: Text | None = None  # and the session's agent browser workspace (cmux/browser.py)
    actor_claude_model: Text | None = None  # set for a claude run, when the session named them: claude's --model
    actor_claude_permission_mode: Text | None = None  # and its --permission-mode
    actor_claude_cwd: Text | None = None  # and the folder Claude Code started in
    actor_codex_model: Text | None = None  # set for a codex run, when the session named them: Codex's model
    actor_codex_sandbox: Text | None = None  # and its sandbox
    actor_codex_cwd: Text | None = None  # and the folder Codex started in
    actor_browser_endpoint: Text | None = None  # a claude or codex run: the session's agent browser, by its CDP address (standalone/browser.py)
    chat: ChatRun | None = None  # set for a chat turn: which turn, what it resumed, and what the agent answered
    events: list[RunEvent]


class RunnerErrorResponse(WireModel):
    code: Text
    error: Text
    hint: Text | None = None


class BackendDefaults(WireModel):
    browser_mode: BrowserMode
    model: Text
    max_response_turns: TurnBudget


class BackendCapabilities(WireModel):
    backend_id: Literal["python"]
    code_tool: Literal["exec_py"]
    browser_modes: Annotated[list[BrowserMode], Field(min_length=1)]
    defaults: BackendDefaults
    instance_id: Text

    @model_validator(mode="after")
    def consistent_backend(self) -> "BackendCapabilities":
        if self.browser_modes != ["headful"]:
            raise ValueError("Python supports a visible browser only.")
        if self.defaults.browser_mode not in self.browser_modes:
            raise ValueError("Default browser mode must be supported.")
        return self


class ReplayArtifacts(WireModel):
    events_path: Text
    replay_path: Text
    run_path: Text
    screenshots_directory: Text
    workspace_path: Text


class ReplayBundle(WireModel):
    version: Literal[3]
    artifacts: ReplayArtifacts
    browser: BrowserState | None = None
    events: list[RunEvent]
    run: RunRecord
    scenario: ScenarioManifest


ModelType = TypeVar("ModelType", bound=WireModel)


def validate_wire(model: type[ModelType], value: Any) -> dict[str, Any]:
    return model.model_validate(value).wire()


def wire_fields(model: type[WireModel]) -> frozenset[str]:
    """The camelCase keys a wire dict of ``model`` may carry: derived from the model, never retyped elsewhere."""
    return frozenset(field.alias or name for name, field in model.model_fields.items())
