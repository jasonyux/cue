"""Settings for a continuous session.

Five roles, five sections of settings.yaml: the summarizer, the belief updater, the proposer and the preference updater each
have their own model and endpoint (``summary``, ``belief``, ``proposer``, ``preference``), and the duplicate filter's embeddings ``embed``. A role's model is
never inherited from anything: the agent's model is the expensive one, so an unset model is an error when a session
starts, not a fallback. A role's endpoint and key come from the most specific place that has them: the start form, the
role's own ``base_url`` in settings.yaml, then the key set of the role's ``provider`` in .env: ``OPENAI_*``,
``ANTHROPIC_*``, ``OPENROUTER_*``, ``LITELLM_*`` or ``VLLM_*``, whose base URL defaults to the provider's own
(``PROVIDER_KEYS``). The observer is configured under ``observer``, the agents under ``actor``, the remote desktop under ``remote_desktop``;
``session.max_session_minutes`` stops a forgotten session. The task agent (the hands) keeps ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY`` / ``CUA_DEFAULT_MODEL``.

Every tunable of a session is defined here and follows the same path: a default below, its place in settings.yaml
(``PLACES``), and a camelCase override from the start form (``with_overrides``); ``wire()`` is what the console and
session.json see, and ``save()`` what the settings page's Save writes: a provider's key or base URL to .env, the rest to
settings.yaml. Nothing is set in two places.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from ..config import SETTINGS_FILE, load_settings, save_environment, save_settings
from .permissions import Default, Level, Permissions, Rules, default_of, rules, session_rules
from .storage.paths import SessionPaths

SCREENSHOT_MODES = ("switch", "always", "off")
PROVIDERS = ("vllm", "openai", "anthropic", "openrouter", "litellm")  # how an endpoint takes its parameters (a role's provider); see LiveModelClient.complete
# A provider's key set: the prefix of the variables that hold its base URL and key, and its base URL when that is unset.
PROVIDER_KEYS = {
    "openai": ("OPENAI", "https://api.openai.com/v1"),
    "anthropic": ("ANTHROPIC", "https://api.anthropic.com/v1/"),  # Anthropic's OpenAI-compatible endpoint
    "openrouter": ("OPENROUTER", "https://openrouter.ai/api/v1"),
    "litellm": ("LITELLM", "http://localhost:4000"),  # a LiteLLM proxy (OpenAI-compatible), at its default address
    "vllm": ("VLLM", "http://localhost:8000/v1"),  # a vLLM server, at vllm serve's default address
}
# The form's fields for a provider's key set (openaiApiKey, vllmBaseUrl, …): for every role on that provider without its own.
PROVIDER_FIELDS = {f"{provider}{field}": (provider, field) for provider in PROVIDER_KEYS for field in ("ApiKey", "BaseUrl")}
SECRETS = {"desktopRemoteToken": "CUA_DESKTOP_REMOTE_TOKEN"}  # a form field whose value goes to .env, never to settings.yaml


def abbreviated(secret: str | None) -> str:
    """A held key as the page may show it, its first and last characters, so the person knows which one; "" when none."""
    if not secret:
        return ""
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 14 else "•••"
REMOTE_DESKTOP_PROVIDERS = ("cua",)  # the actor's remote desktop (remote_desktop.provider); see actor/desktop/remote.py
DESKTOP_SOURCES = ("local", "remote")  # the desktops a run from the live page gets (actor.desktop_source): either or both
PROPOSER_INPUTS = ("tasks", "worklog", "snapshots", "current_window", "feedback", "chat", "preferences")  # what the proposer can be shown (proposer.inputs)
ACTOR_AGENTS = ("desktop", "cmux", "claude", "codex")  # the agents a session sets up for its runs (actor.agents): desktop is the
                                # OpenAI model driving exec_py, cmux is Claude Code in a cmux workspace (actor/cmux), claude and codex are
                                # Claude Code and Codex started by the backend itself (actor/standalone); a run picks one of them
CODEX_SANDBOXES = ("read-only", "workspace-write", "full-access")  # Codex's sandbox for its own commands, as codex --sandbox spells them
CMUX_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")  # claude --permission-mode
ACTOR_YIELD_S_DEFAULT = 20.0  # the live actor's exec_py yields a still-running cell after this long (actor.yield_s)
MAX_PROPOSALS_DEFAULT = 3  # ranked proposals per round (proposer.max_proposals); the console shows the first
MAX_PROPOSALS_CAP = 5      # hard cap on that setting; the console reads it from the status payload
IDLE_S_RANGE = (3.0, 600.0)  # the idle trigger's pause (proposer.idle_s), in seconds


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() else default


def _screenshot_mode(value: str | None, default: str = "switch") -> str:
    if value is None:
        return default
    key = value.strip().lower()
    if key in ("1", "true", "yes", "on"):
        return "switch"
    if key in ("0", "false", "no"):
        return "off"
    if key not in SCREENSHOT_MODES:
        raise ValueError(f"screenshotMode must be one of {', '.join(SCREENSHOT_MODES)}, not {value!r}.")
    return key


def _subset(name: str, value: Any, choices: tuple[str, ...], default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated string or a list of names → the non-empty subset of ``choices``, in their order."""
    if value is None:
        return default
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list | tuple) or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{name} must be a comma-separated string or a list.")
    chosen = {item.strip().lower() for item in items if item.strip()}
    if not chosen or chosen - set(choices):
        raise ValueError(f"{name} must name at least one of {', '.join(choices)}, not {value!r}.")
    return tuple(item for item in choices if item in chosen)


def _proposer_inputs(value: Any) -> tuple[str, ...]:
    return _subset("proposerInputs", value, PROPOSER_INPUTS, PROPOSER_INPUTS)


def _desktop_source(value: Any) -> tuple[str, ...]:
    return _subset("actorDesktopSource", value, DESKTOP_SOURCES, ("local",))


def _actor_agents(value: Any) -> tuple[str, ...]:
    return _subset("actorAgents", value, ACTOR_AGENTS, ("claude",))


def _codex_sandbox(value: str, key: str) -> str:
    chosen = value.strip()
    if chosen not in CODEX_SANDBOXES:
        raise ValueError(f"{key} must be one of {', '.join(CODEX_SANDBOXES)}, not {value!r}.")
    return chosen


def _permission_mode(value: str, key: str) -> str:
    chosen = value.strip()
    if chosen not in CMUX_PERMISSION_MODES:
        raise ValueError(f"{key} must be one of {', '.join(CMUX_PERMISSION_MODES)}, not {value!r}.")
    return chosen


def _remote_provider(value: str | None) -> str | None:
    """None for no remote desktop; otherwise a name from REMOTE_DESKTOP_PROVIDERS."""
    chosen = (value or "").strip().lower() or None
    if chosen is not None and chosen not in REMOTE_DESKTOP_PROVIDERS:
        raise ValueError(f"desktopRemoteProvider must be one of {', '.join(REMOTE_DESKTOP_PROVIDERS)}, not {value!r}.")
    return chosen


def _provider(value: str | None) -> str:
    chosen = (value or "vllm").strip().lower()
    if chosen not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}, not {value!r}.")
    return chosen


def _keys(provider: str, base_url: str | None = None, api_key: str | None = None,
          given: dict[str, str] | None = None) -> tuple[str, str | None]:
    """A role's base URL and key, the most specific first: the role's own (the start form, or its ``base_url`` in
    settings.yaml), then the provider's key set (``given`` on the form, else its variables in .env) and its usual base
    URL. The key is None when nothing has one (a local server that takes none)."""
    name, url = PROVIDER_KEYS[provider]
    given = given or {}
    return base_url or given.get("BaseUrl") or _env(f"{name}_BASE_URL") or url, api_key or given.get("ApiKey") or _env(f"{name}_API_KEY")


@dataclass(frozen=True)
class ModelEndpoint:
    model: str = ""               # empty: not configured; a session cannot start with it
    base_url: str | None = None   # loaded, always set: the role's own (settings.yaml), else its provider's (.env, else PROVIDER_KEYS)
    api_key: str | None = None    # None: no key (a local endpoint that takes none)
    provider: str = "vllm"        # vllm: temperature and Qwen's thinking flag; openai (through the Responses API): reasoning effort, no temperature;
                                  # litellm: as anthropic for a Claude model, else as openai (on Chat Completions);
                                  # anthropic (Anthropic's OpenAI-compatible API, a Claude model behind a proxy, or a bedrock-mantle URL,
                                  # called through the Messages API): adaptive thinking, no temperature; openrouter: OpenRouter's own reasoning switch, for any model it serves
    stream: bool = True           # stream the reply and assemble it (a proxy's first-byte timeout cannot cut a long think); off for an endpoint that cannot stream

    @classmethod
    def resolved(cls, provider: str = "vllm") -> ModelEndpoint:
        """No model yet, on ``provider``: at its key set's address, with its key."""
        base_url, api_key = _keys(provider)
        return cls(base_url=base_url, api_key=api_key, provider=provider)


@dataclass(frozen=True)
class LiveSettings:
    summary: ModelEndpoint
    belief: ModelEndpoint
    proposer: ModelEndpoint
    embed: ModelEndpoint = ModelEndpoint()  # the embeddings endpoint (model, base URL, key) the proposer's duplicate filter uses
    preference: ModelEndpoint = ModelEndpoint()  # the preference updater's (preference/update.py); its model is required at a start like the others'
    snapshot_interval_s: float = 10.0
    summary_interval_s: float = 60.0   # fast summarizer: turns each minute of snapshots into one worklog line
    proposer_interval_s: float = 300.0    # proposer: reads the worklog plus the current window and decides
    belief_interval_s: float = 300.0      # belief updater: folds new worklog lines into the task list
    belief_thinking: bool = True
    belief_max_tokens: int = 0
    belief_stale_days: int = 7            # an active task not seen for this long is paused by the store
    belief_done_step_days: int = 21       # finished entries older than this are hidden in the console, behind "show all"; never deleted
    belief_trash_days: int = 7            # deleted tasks and entries stay recoverable this long, then are dropped
    belief_context_summaries: int = 30    # earlier worklog lines the belief updater sees before the new ones: the arc of the work
    belief_similar_threshold: float = 0.8  # cosine at or above which an entry the model adds is held back once with its lookalikes; 0 = off
    preference_thinking: bool = True      # preference updater: on its timer, the model free to leave the file as it is
    preference_max_tokens: int = 0
    preference_cap_tokens: int = 400      # the preferences file may not exceed this many cl100k tokens; a longer write is rejected
    preference_context_summaries: int = 15  # the latest worklog lines the updater is shown
    preference_context_decisions: int = 10  # earlier decisions shown before the new reactions
    preference_search_rounds: int = 3     # history searches a tick may make; 0 leaves the tool out
    preference_interval_s: float = 300.0  # preference updater: a tick this often while a session runs, and one at its stop
    preference_min_feedback: int = 1      # reactions (a decision, a thumbs) a tick needs; fewer wait for the next
    stream_lookback_days: int = 30        # how far back a stage's window reads the stream for what stood at its end (worklog, decisions, runs)
    summary_thinking: bool = False     # reasoning models: keep the summarizer fast
    proposer_thinking: bool = True        # let the proposer reason about intent before deciding
    summary_screenshots: bool = False  # attach the latest window screenshots to summarizer calls
    proposer_screenshots: bool = False    # attach the latest window screenshots to proposer calls
    proposer_inputs: tuple[str, ...] = PROPOSER_INPUTS  # what the proposer is shown, any of: tasks, worklog (past
                                          # summaries), snapshots (this window's raw rows), current_window (the newest tree),
                                          # feedback (recent decisions on proposals, across sessions), chat (what the person
                                          # lately asked the assistant in the chat, and what it answered)
    proposer_feedback_rejected: int = 6   # the feedback source: the last N rejected proposals, those with a typed reason first,
    proposer_feedback_accepted: int = 3   # the last N accepted,
    proposer_feedback_recent: int = 2     # and the top proposal of the last N rounds of this session nobody has answered yet
    proposer_search_rounds: int = 5       # the history search tool (storage/search.py): up to this many searches a round; 0 leaves the tool out
    proposer_context_summaries: int = 20  # the worklog source: the session's last N worklog lines, as actor_context_summaries is a run's (the summarizer's own context is fixed at 5)
    proposer_dedup: bool = False          # the guardrail after generation: drop a proposal that embeds too close to one already decided or shown
    proposer_dedup_threshold: float = 0.8  # cosine similarity at or above which a proposal is a duplicate
    proposer_dedup_not_now_hours: float = 24.0  # a "not now" rejection counts as a reference for this long
    proposer_dedup_session_hours: float = 24.0  # this session's own earlier proposals count for this long
    max_proposals: int = MAX_PROPOSALS_DEFAULT
    # The idle trigger (proposer/idle.py), a second proposer beside the timer: like an editor's autocomplete, one quick
    # proposal on the current screen when the person stops typing and moving the mouse for idle_s after working.
    proposer_idle_trigger: bool = False   # on or off; the console's menu switches it for the running session too
    proposer_idle_s: float = 10.0         # seconds without keyboard or mouse input that fire it
    proposer_idle_cooldown_s: float = 30.0  # at least this long between two fires
    proposer_idle_away_s: float = 120.0   # still no input this long after a fire: the person is away (shown on the console)
    proposer_idle_context_s: float = 120.0  # the raw observations it reads: this many seconds back from the pause
    proposer_idle_proposals: int = 1      # proposals per fire
    proposer_idle_thinking: bool = True   # the model's own default effort (~3s with gpt-6-luna); off sends the provider's
                                          # no-thinking switch (on OpenAI the lowest effort the model takes), which some models reject
    summary_max_tokens: int = 0       # output tokens per call; 0 = automatic (model_client picks by thinking on/off)
    proposer_max_tokens: int = 0
    screenshot_mode: str = "switch"    # observer: "switch" (on app change), "always" (every distinct snapshot), "off"
    screenshot_max_side: int = 1024    # images sent to models are downscaled to this many pixels on the long side
    max_session_minutes: float = 120.0  # auto-stop; 0 disables the limit
    permissions_default: Default = Level.LOOK  # what the observer may read of an app no rule names (permissions.default): a level, or ask
    permissions_apps: dict[str, Level] = dataclass_field(default_factory=dict)   # app name or bundle id (globs allowed) → level (permissions.apps)
    permissions_sites: dict[str, Level] = dataclass_field(default_factory=dict)  # a domain or a glob on the address → level, in any browser (permissions.sites)
    max_elements: int = 400
    notify: bool = True
    actor_yield_s: float = ACTOR_YIELD_S_DEFAULT  # a run from the live page carries this to the actor's exec_py
    actor_context_summaries: int = 12  # how many of the session's last worklog lines a run is given as context
    actor_desktop_source: tuple[str, ...] = ("local",)  # the desktops a run gets: local, remote, or both (then exec_py takes a target)
    remote_desktop_provider: str | None = None  # the remote desktop, by provider name; needed when the source includes remote
    remote_desktop_url: str | None = None       # where the provider's runtime is reached (the cua bridge: ws:// or wss://)
    remote_desktop_token: str | None = None     # its shared secret; environment only, never on the wire
    actor_agents: tuple[str, ...] = ("claude",)  # the agents runs can use: any of claude, desktop and cmux (with more than one, each run picks one)
    actor_cmux_model: str | None = None         # the cmux agent's model, as claude --model takes it; None: Claude Code's default
    actor_cmux_permission_mode: str = "auto"    # its claude --permission-mode. A headless run cannot answer a permission
                                                # prompt, so "manual" leaves it unable to use most tools
    actor_cmux_cwd: str | None = None           # the folder Claude Code starts in; None: the run's own workspace folder
    actor_claude_model: str | None = None       # the claude agent's model, as claude --model takes it; None: Claude Code's default
    actor_claude_permission_mode: str = "auto"  # its claude --permission-mode; whatever it holds back, the person answers on the run's card
    actor_claude_cwd: str | None = None         # the folder Claude Code starts in; None: the run's own workspace folder
    actor_codex_model: str | None = None        # the codex agent's model; None: Codex's default
    actor_codex_sandbox: str = "workspace-write"  # its sandbox: files in its folder, no network for its shell; Codex's reviewer judges more
    actor_codex_cwd: str | None = None          # the folder Codex starts in; None: the run's own workspace folder

    @property
    def permissions(self) -> Permissions:
        return Permissions(self.permissions_default, dict(self.permissions_apps), dict(self.permissions_sites))

    def permissions_with(self, path: Path) -> Permissions:
        """The rules plus a session's own answers (``session_rules``), as the observer and the status apply them."""
        base = self.permissions
        return Permissions(base.default, {**base.apps, **session_rules(path)}, base.sites)

    @property
    def effective_screenshot_mode(self) -> str:
        """Models that want screenshots need one per distinct snapshot, not only on app switches."""
        if self.summary_screenshots or self.proposer_screenshots:
            return "always"
        return self.screenshot_mode

    def require_models(self) -> None:
        """Every live role must name its model explicitly; there is no fallback to another role's model."""
        for role, endpoint in (("summary", self.summary), ("belief", self.belief), ("proposer", self.proposer), ("preference", self.preference)):
            if not endpoint.model:
                raise ValueError(f"The {role} model is not set. Set {role}.model in settings.yaml (with its provider) or fill it "
                                 "on the form; live loops never fall back to another model.")
        if self.proposer_dedup and not self.embed.model:
            raise ValueError("The proposer's duplicate filter is on but no embeddings model is set. Set embed.model in settings.yaml "
                             "(with its provider) or fill it on the form, or turn the filter off.")

    @classmethod
    def load(cls) -> LiveSettings:
        """settings.yaml over the defaults (``PLACES``), each role's endpoint and key from its provider's key set in .env;
        the remote desktop's token from the environment too (.env), never on the wire."""
        data = load_settings()
        given = {key: _dig(data.get(section), name) for key, (section, name) in PLACES.items()}  # missing or null: unset
        endpoint = ModelEndpoint.resolved()
        return cls(summary=endpoint, belief=endpoint, proposer=endpoint, embed=endpoint, preference=endpoint,
                   remote_desktop_token=_env("CUA_DESKTOP_REMOTE_TOKEN")).with_overrides(given)

    def require_desktops(self) -> None:
        """A run's desktops must exist: ``remote`` in the source needs a provider, its URL and its token."""
        if "remote" in self.actor_desktop_source and not (self.remote_desktop_provider and self.remote_desktop_url and self.remote_desktop_token):
            raise ValueError("actorDesktopSource includes remote: set remote_desktop.provider and remote_desktop.url in settings.yaml "
                             "and CUA_DESKTOP_REMOTE_TOKEN in .env.")

    def with_overrides(self, values: dict[str, Any]) -> LiveSettings:
        """Apply a start request (camelCase keys) with validation; unknown keys are rejected."""
        given: dict[str, dict[str, str]] = {}  # provider → its key and base URL from the form (PROVIDER_FIELDS)
        for key, (provider, field) in PROVIDER_FIELDS.items():
            if values.get(key) is None or values.get(key) == "":
                continue
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"{key} must be a nonempty string.")
            given.setdefault(provider, {})[field] = values[key].strip()
        values = {key: value for key, value in values.items() if key not in PROVIDER_FIELDS}
        minimums = {
            "snapshot_interval_s": 2.0,
            "summary_interval_s": 10.0,
            "proposer_interval_s": 15.0,
            "belief_interval_s": 15.0,
            "preference_interval_s": 15.0,
        }
        changes: dict[str, Any] = {}
        endpoints: dict[str, dict[str, Any]] = {"summary": {}, "proposer": {}, "belief": {}, "embed": {}, "preference": {}}
        for key, value in values.items():
            if value is None or value == "":
                continue
            if key in ENDPOINT_FIELDS:
                role, field = ENDPOINT_FIELDS[key]
                if field == "stream":
                    if not isinstance(value, bool):
                        raise ValueError(f"{key} must be a boolean.")
                    endpoints[role][field] = value
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} must be a nonempty string.")
                endpoints[role][field] = _provider(value) if field == "provider" else value.strip()
                continue
            if key not in FIELDS:
                raise ValueError(f"Unknown live session setting: {key}")
            field = FIELDS[key]
            if field == "permissions_default":
                value = default_of(value, key)
            elif field in ("permissions_apps", "permissions_sites"):
                value = rules(value, key)
            elif field == "screenshot_mode":
                if not isinstance(value, str):
                    raise ValueError("screenshotMode must be a string.")
                value = _screenshot_mode(value)
            elif field == "proposer_inputs":
                value = _proposer_inputs(value)
            elif field == "actor_desktop_source":
                value = _desktop_source(value)
            elif field == "actor_agents":
                value = _actor_agents(value)
            elif field in ("actor_cmux_model", "actor_cmux_permission_mode", "actor_cmux_cwd", "actor_claude_model", "actor_claude_permission_mode", "actor_claude_cwd",
                           "actor_codex_model", "actor_codex_sandbox", "actor_codex_cwd"):
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string.")
                value = (_permission_mode(value, key) if field.endswith("_permission_mode") else _codex_sandbox(value, key) if field == "actor_codex_sandbox"
                         else value.strip())
            elif field in minimums:
                value = float(value)
                if value < minimums[field]:
                    raise ValueError(f"{key} must be at least {minimums[field]:g} seconds.")
            elif field == "remote_desktop_url":
                if not isinstance(value, str):
                    raise ValueError("desktopRemoteUrl must be a string.")
                value = value.strip()
            elif field == "remote_desktop_provider":
                if not isinstance(value, str):
                    raise ValueError("desktopRemoteProvider must be a string.")
                value = _remote_provider(value)
            elif field == "remote_desktop_token":
                if not isinstance(value, str):
                    raise ValueError("desktopRemoteToken must be a string.")
                value = value.strip()
            elif field == "actor_yield_s":
                value = float(value)
                if value <= 0:
                    raise ValueError("actorYieldS must be positive.")
            elif field == "max_session_minutes":
                value = float(value)
                if value < 0:
                    raise ValueError("maxSessionMinutes must be 0 (no limit) or positive.")
            elif field == "max_proposals":
                value = int(value)
                if not 1 <= value <= MAX_PROPOSALS_CAP:
                    raise ValueError(f"maxProposals must be between 1 and {MAX_PROPOSALS_CAP}.")
            elif field == "proposer_idle_proposals":
                value = int(value)
                if not 1 <= value <= MAX_PROPOSALS_CAP:
                    raise ValueError(f"proposerIdleProposals must be between 1 and {MAX_PROPOSALS_CAP}.")
            elif field == "proposer_idle_s":
                value = float(value)
                if not IDLE_S_RANGE[0] <= value <= IDLE_S_RANGE[1]:
                    raise ValueError(f"proposerIdleS must be between {IDLE_S_RANGE[0]:g} and {IDLE_S_RANGE[1]:g} seconds.")
            elif field in ("proposer_idle_cooldown_s", "proposer_idle_away_s", "proposer_idle_context_s"):
                value = float(value)
                if value <= 0:
                    raise ValueError(f"{key} must be positive.")
            elif field == "proposer_dedup_threshold":
                value = float(value)
                if not 0 < value <= 1:
                    raise ValueError("proposerDedupThreshold must be above 0 and at most 1.")
            elif field in ("proposer_dedup_not_now_hours", "proposer_dedup_session_hours"):
                value = float(value)
                if value <= 0:
                    raise ValueError(f"{key} must be positive.")
            elif field in ("summary_max_tokens", "proposer_max_tokens", "belief_max_tokens", "preference_max_tokens"):
                value = int(value)
                if value < 0:
                    raise ValueError(f"{key} must be 0 (automatic) or positive.")
            elif field in ("proposer_search_rounds", "preference_search_rounds"):
                value = int(value)
                if value < 0:
                    raise ValueError(f"{key} must be 0 (no search tool) or positive.")
            elif field == "belief_context_summaries":
                value = int(value)
                if value < 0:
                    raise ValueError(f"{key} must be 0 or positive.")
            elif field == "belief_similar_threshold":
                value = float(value)
                if not 0 <= value <= 1:
                    raise ValueError("beliefSimilarThreshold must be between 0 (off) and 1.")
            elif field in ("max_elements", "proposer_context_summaries", "screenshot_max_side", "belief_stale_days",
                           "belief_done_step_days", "belief_trash_days", "stream_lookback_days", "proposer_feedback_rejected",
                           "proposer_feedback_accepted", "proposer_feedback_recent", "actor_context_summaries", "preference_cap_tokens",
                           "preference_context_summaries", "preference_context_decisions", "preference_min_feedback"):
                value = int(value)
                if value <= 0:
                    raise ValueError(f"{key} must be positive.")
            elif not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
            changes[field] = value
        for role, fields in endpoints.items():
            provider = fields.get("provider", getattr(self, role).provider)
            if "provider" in fields or provider in given:  # another provider, or its key set given: the endpoint and key again
                fields["base_url"], fields["api_key"] = _keys(provider, fields.get("base_url"), fields.get("api_key"), given.get(provider))
            if fields:
                changes[role] = replace(getattr(self, role), **fields)
        return replace(self, **changes)

    def wire(self) -> dict[str, Any]:
        """camelCase view for the console and session.json (a key only abbreviated, never whole)."""
        return {
            "summaryModel": self.summary.model, "summaryBaseUrl": self.summary.base_url, "summaryProvider": self.summary.provider,
            "proposerModel": self.proposer.model, "proposerBaseUrl": self.proposer.base_url, "proposerProvider": self.proposer.provider,
            "beliefModel": self.belief.model, "beliefBaseUrl": self.belief.base_url, "beliefProvider": self.belief.provider,
            "summaryStream": self.summary.stream, "proposerStream": self.proposer.stream, "beliefStream": self.belief.stream,
            "embedModel": self.embed.model, "embedBaseUrl": self.embed.base_url, "embedProvider": self.embed.provider,
            # Which providers' keys the backend's environment holds, abbreviated: the settings page says a blank key uses it.
            "providerKeys": {provider: abbreviated(_env(f"{name}_API_KEY")) for provider, (name, _url) in PROVIDER_KEYS.items()},
            **{f"{provider}BaseUrl": _env(f"{name}_BASE_URL", url) for provider, (name, url) in PROVIDER_KEYS.items()},
            "proposerDedup": self.proposer_dedup, "proposerDedupThreshold": self.proposer_dedup_threshold,
            "proposerDedupNotNowHours": self.proposer_dedup_not_now_hours, "proposerDedupSessionHours": self.proposer_dedup_session_hours,
            "snapshotIntervalS": self.snapshot_interval_s, "summaryIntervalS": self.summary_interval_s,
            "proposerIntervalS": self.proposer_interval_s, "summaryThinking": self.summary_thinking,
            "proposerThinking": self.proposer_thinking, "summaryScreenshots": self.summary_screenshots,
            "proposerScreenshots": self.proposer_screenshots, "proposerInputs": list(self.proposer_inputs),
            "maxProposals": self.max_proposals,
            "proposerIdleTrigger": self.proposer_idle_trigger, "proposerIdleS": self.proposer_idle_s,
            "proposerIdleCooldownS": self.proposer_idle_cooldown_s, "proposerIdleAwayS": self.proposer_idle_away_s,
            "proposerIdleContextS": self.proposer_idle_context_s, "proposerIdleProposals": self.proposer_idle_proposals,
            "proposerIdleThinking": self.proposer_idle_thinking,
            "beliefIntervalS": self.belief_interval_s, "beliefThinking": self.belief_thinking, "beliefMaxTokens": self.belief_max_tokens,
            "beliefStaleDays": self.belief_stale_days, "beliefContextSummaries": self.belief_context_summaries,
            "beliefSimilarThreshold": self.belief_similar_threshold,
            "beliefDoneStepDays": self.belief_done_step_days, "beliefTrashDays": self.belief_trash_days, "streamLookbackDays": self.stream_lookback_days,
            "preferenceModel": self.preference.model, "preferenceBaseUrl": self.preference.base_url, "preferenceProvider": self.preference.provider,
            "preferenceStream": self.preference.stream, "preferenceThinking": self.preference_thinking, "preferenceMaxTokens": self.preference_max_tokens,
            "preferenceCapTokens": self.preference_cap_tokens, "preferenceContextSummaries": self.preference_context_summaries,
            "preferenceContextDecisions": self.preference_context_decisions, "preferenceSearchRounds": self.preference_search_rounds,
            "preferenceIntervalS": self.preference_interval_s, "preferenceMinFeedback": self.preference_min_feedback,
            "summaryMaxTokens": self.summary_max_tokens, "proposerMaxTokens": self.proposer_max_tokens,
            "proposerFeedbackRejected": self.proposer_feedback_rejected, "proposerFeedbackAccepted": self.proposer_feedback_accepted,
            "proposerFeedbackRecent": self.proposer_feedback_recent,
            "proposerSearchRounds": self.proposer_search_rounds, "screenshotMode": self.screenshot_mode,
            "effectiveScreenshotMode": self.effective_screenshot_mode, "screenshotMaxSide": self.screenshot_max_side,
            "maxSessionMinutes": self.max_session_minutes,
            "permissionsDefault": str(self.permissions_default),  # the levels as their names: what the YAML and the JSON hold
            "permissionsApps": {name: str(level) for name, level in self.permissions_apps.items()},
            "permissionsSites": {name: str(level) for name, level in self.permissions_sites.items()},
            "maxElements": self.max_elements, "notify": self.notify, "proposerContextSummaries": self.proposer_context_summaries,
            "actorYieldS": self.actor_yield_s, "actorContextSummaries": self.actor_context_summaries,
            "actorDesktopSource": list(self.actor_desktop_source),
            "desktopRemoteProvider": self.remote_desktop_provider,
            "desktopRemoteUrl": self.remote_desktop_url,
            "desktopRemoteTokenHint": abbreviated(self.remote_desktop_token),
            "actorAgents": list(self.actor_agents), "actorCmuxModel": self.actor_cmux_model, "actorCmuxPermissionMode": self.actor_cmux_permission_mode,
            "actorCmuxCwd": self.actor_cmux_cwd,
            "actorClaudeModel": self.actor_claude_model, "actorClaudePermissionMode": self.actor_claude_permission_mode,
            "actorClaudeCwd": self.actor_claude_cwd,
            "actorCodexModel": self.actor_codex_model, "actorCodexSandbox": self.actor_codex_sandbox, "actorCodexCwd": self.actor_codex_cwd,
        }

    def save(self, values: dict[str, Any]) -> None:
        """The settings page's changes, checked as a start would check them: a provider's key or base URL to .env, the rest
        to settings.yaml as the settings hold them (a blank one as null, which reads as unset); then both are read again."""
        held = self.with_overrides(values).wire()
        placed = {key: value for key, value in values.items() if key in PLACES}
        if placed:
            data = load_settings()
            for key, value in placed.items():
                section, name = PLACES[key]
                *outer, last = name.split(".")
                target = data.setdefault(section, {})
                for step in outer:
                    if not isinstance(target.get(step), dict):
                        target[step] = {}  # an older file's one number where a block now goes
                    target = target[step]
                target[last] = None if value is None or value == "" else held[key]
            save_settings(data)
        save_environment({**{f"{PROVIDER_KEYS[provider][0]}_{'API_KEY' if field == 'ApiKey' else 'BASE_URL'}": values[key] or ""
                             for key, (provider, field) in PROVIDER_FIELDS.items() if key in values},
                          **{name: values[key] or "" for key, name in SECRETS.items() if key in values}})


# The start form's camelCase keys: a role's endpoint fields as (role, field), and the settings' fields.
ENDPOINT_FIELDS = {
    "summaryModel": ("summary", "model"), "summaryBaseUrl": ("summary", "base_url"), "summaryApiKey": ("summary", "api_key"),
    "proposerModel": ("proposer", "model"), "proposerBaseUrl": ("proposer", "base_url"), "proposerApiKey": ("proposer", "api_key"),
    "beliefModel": ("belief", "model"), "beliefBaseUrl": ("belief", "base_url"), "beliefApiKey": ("belief", "api_key"),
    "summaryProvider": ("summary", "provider"), "proposerProvider": ("proposer", "provider"), "beliefProvider": ("belief", "provider"),
    "summaryStream": ("summary", "stream"), "proposerStream": ("proposer", "stream"), "beliefStream": ("belief", "stream"),
    "embedModel": ("embed", "model"), "embedBaseUrl": ("embed", "base_url"), "embedApiKey": ("embed", "api_key"),
    "embedProvider": ("embed", "provider"),
    "preferenceModel": ("preference", "model"), "preferenceBaseUrl": ("preference", "base_url"), "preferenceApiKey": ("preference", "api_key"),
    "preferenceProvider": ("preference", "provider"), "preferenceStream": ("preference", "stream"),
}
FIELDS = {
    "snapshotIntervalS": "snapshot_interval_s", "summaryIntervalS": "summary_interval_s",
    "proposerIntervalS": "proposer_interval_s", "summaryThinking": "summary_thinking", "proposerThinking": "proposer_thinking",
    "summaryScreenshots": "summary_screenshots", "proposerScreenshots": "proposer_screenshots",
    "proposerInputs": "proposer_inputs",
    "beliefIntervalS": "belief_interval_s", "beliefThinking": "belief_thinking", "beliefMaxTokens": "belief_max_tokens",
    "beliefStaleDays": "belief_stale_days", "beliefDoneStepDays": "belief_done_step_days",
    "beliefContextSummaries": "belief_context_summaries", "beliefSimilarThreshold": "belief_similar_threshold",
    "beliefTrashDays": "belief_trash_days",
    "preferenceThinking": "preference_thinking", "preferenceMaxTokens": "preference_max_tokens", "preferenceCapTokens": "preference_cap_tokens",
    "preferenceContextSummaries": "preference_context_summaries", "preferenceContextDecisions": "preference_context_decisions",
    "preferenceSearchRounds": "preference_search_rounds",
    "preferenceIntervalS": "preference_interval_s", "preferenceMinFeedback": "preference_min_feedback",
    "streamLookbackDays": "stream_lookback_days",
    "maxProposals": "max_proposals", "summaryMaxTokens": "summary_max_tokens",
    "proposerMaxTokens": "proposer_max_tokens", "proposerFeedbackRejected": "proposer_feedback_rejected",
    "proposerFeedbackAccepted": "proposer_feedback_accepted", "proposerFeedbackRecent": "proposer_feedback_recent",
    "proposerSearchRounds": "proposer_search_rounds",
    "proposerDedup": "proposer_dedup", "proposerDedupThreshold": "proposer_dedup_threshold",
    "proposerDedupNotNowHours": "proposer_dedup_not_now_hours", "proposerDedupSessionHours": "proposer_dedup_session_hours",
    "proposerIdleTrigger": "proposer_idle_trigger", "proposerIdleS": "proposer_idle_s", "proposerIdleCooldownS": "proposer_idle_cooldown_s",
    "proposerIdleAwayS": "proposer_idle_away_s", "proposerIdleContextS": "proposer_idle_context_s",
    "proposerIdleProposals": "proposer_idle_proposals", "proposerIdleThinking": "proposer_idle_thinking",
    "screenshotMode": "screenshot_mode", "screenshotMaxSide": "screenshot_max_side",
    "maxSessionMinutes": "max_session_minutes", "maxElements": "max_elements",
    "permissionsDefault": "permissions_default", "permissionsApps": "permissions_apps", "permissionsSites": "permissions_sites",
    "notify": "notify", "proposerContextSummaries": "proposer_context_summaries", "actorYieldS": "actor_yield_s",
    "actorContextSummaries": "actor_context_summaries",
    "actorDesktopSource": "actor_desktop_source",
    "desktopRemoteProvider": "remote_desktop_provider", "desktopRemoteUrl": "remote_desktop_url", "desktopRemoteToken": "remote_desktop_token",
    "actorAgents": "actor_agents", "actorCmuxModel": "actor_cmux_model", "actorCmuxPermissionMode": "actor_cmux_permission_mode", "actorCmuxCwd": "actor_cmux_cwd",
    "actorClaudeModel": "actor_claude_model", "actorClaudePermissionMode": "actor_claude_permission_mode", "actorClaudeCwd": "actor_claude_cwd",
    "actorCodexModel": "actor_codex_model", "actorCodexSandbox": "actor_codex_sandbox", "actorCodexCwd": "actor_codex_cwd",
}
# Where settings.yaml keeps each: under its section with the section's name dropped (summary_interval_s is summary.interval_s,
# remote_desktop_url is remote_desktop.url; a role's model, provider, base_url and stream under the role's name), the fields named
# without one under the observer's, the proposer's or the session's; those in NESTED one level deeper (proposer.feedback_window.rejected).
# No key is ever in it: a role's own comes from the form only, a provider's and the remote desktop's token (SECRETS) from .env.
SECTIONS = ("summary", "belief", "proposer", "preference", "embed", "actor", "remote_desktop", "permissions")
SECTION_OF = {"snapshot_interval_s": "observer", "screenshot_mode": "observer", "screenshot_max_side": "observer",
              "max_elements": "observer", "max_proposals": "proposer", "notify": "proposer", "max_session_minutes": "session",
              "stream_lookback_days": "session"}


NESTED = {"proposer_feedback_rejected": "feedback_window.rejected", "proposer_feedback_accepted": "feedback_window.accepted",
          "proposer_feedback_recent": "feedback_window.recent"}


def _dig(data: Any, path: str) -> Any:
    """The value at a dotted path into nested dicts; None when any step is missing."""
    for step in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(step)
    return data


def _place(field: str) -> tuple[str, str]:
    section = next((name for name in SECTIONS if field.startswith(f"{name}_")), None)
    if section and field in NESTED:
        return (section, NESTED[field])
    return (section, field[len(section) + 1:]) if section else (SECTION_OF[field], field)


PLACES = {**{key: (role, field) for key, (role, field) in ENDPOINT_FIELDS.items() if field != "api_key"},
          **{key: _place(field) for key, field in FIELDS.items() if key not in SECRETS}}


def agent_rules(folder: Path) -> Rules:
    """The permissions as a session's agent applies them (``Rules``): the file's and the session's own answers, read again when
    either changes, and the session's asks file."""
    paths = SessionPaths(folder)
    return Rules(lambda: LiveSettings.load().permissions_with(paths.settings), (SETTINGS_FILE, paths.settings), paths.asks)
