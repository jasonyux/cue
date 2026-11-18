"""Session settings: defaults, settings.yaml, and start-form overrides."""


from textwrap import dedent
from unittest.mock import AsyncMock

import httpx
import pytest
from dotenv import dotenv_values
from live_helpers import fake_settings, make_manager
from ruamel.yaml import YAML

from app import config as app_config
from app.api import create_app
from app.live.config import PLACES, PROVIDER_FIELDS, LiveSettings, ModelEndpoint
from app.live.permissions import Level, Permissions, Reason, write_session_rules
from app.runner import RunnerManager


def saved(tmp_path, text: str) -> LiveSettings:
    """The settings with this settings.yaml (conftest points the repo root at tmp_path)."""
    (tmp_path / "settings.yaml").write_text(dedent(text))
    return LiveSettings.load()


def test_settings_from_the_file_and_overrides(tmp_path):
    settings = saved(tmp_path, """
        summary: {model: qwen, base_url: http://vllm/v1}
        belief: {model: qwen}
        proposer: {thinking: false, provider: openai, stream: false, dedup: true, dedup_not_now_hours: 12}
        embed: {model: embed-1, base_url: http://e/v1}
        permissions: {default: block, apps: {Slack: look, com.apple.systempreferences: use}, sites: {chase.com: block}}
    """)
    assert settings.summary == ModelEndpoint(model="qwen", base_url="http://vllm/v1")
    assert settings.proposer == ModelEndpoint(model="", base_url="https://api.openai.com/v1", provider="openai", stream=False)  # never the task agent's model: unset stays unset
    assert settings.summary.stream is True and settings.wire()["proposerStream"] is False
    assert settings.proposer_dedup and settings.embed == ModelEndpoint(model="embed-1", base_url="http://e/v1")
    assert (settings.proposer_dedup_threshold, settings.proposer_dedup_not_now_hours, settings.proposer_dedup_session_hours) == (0.8, 12.0, 24.0)
    assert settings.wire()["embedModel"] == "embed-1" and settings.wire()["proposerDedupNotNowHours"] == 12.0 and "embedApiKey" not in settings.wire()
    with pytest.raises(ValueError, match="no embeddings model"):  # the filter on needs an embeddings model
        LiveSettings(summary=settings.summary, belief=settings.belief, proposer=settings.summary, preference=settings.summary, proposer_dedup=True).require_models()
    with pytest.raises(ValueError, match="proposer model is not set"):
        settings.require_models()
    with pytest.raises(ValueError, match="preference model is not set"):  # every role, the preference updater's too
        settings.with_overrides({"proposerModel": "qwen"}).require_models()
    settings.with_overrides({"proposerModel": "qwen", "preferenceModel": "qwen"}).require_models()  # the belief role on vllm serve's own address
    assert (settings.permissions.level("Slack"), settings.permissions.level("System Settings", "com.apple.systempreferences")) == ("look", "use")
    assert settings.permissions.level("Notes") == "block" and settings.permissions.level("Slack", address="online.chase.com") == "block"
    assert settings.wire()["permissionsApps"] == {"Slack": "look", "com.apple.systempreferences": "use"} and settings.wire()["permissionsDefault"] == "block"
    asking = settings.with_overrides({"permissionsDefault": "ask"})
    assert asking.permissions_default == "ask" and asking.wire()["permissionsDefault"] == "ask" and asking.permissions.decide("Notes") == (Level.BLOCK, Reason.ASK)
    write_session_rules(tmp_path / "s.yaml", {"Notes": "look"})  # a session's own answer, merged over the file's rules for that session
    assert asking.permissions_with(tmp_path / "s.yaml").decide("Notes") == (Level.LOOK, Reason.APP) and asking.permissions_with(tmp_path / "none.yaml").decide("Notes") == (Level.BLOCK, Reason.ASK)
    with pytest.raises(ValueError, match="permissionsApps Slack must be one of block, look, use"):
        settings.with_overrides({"permissionsApps": "Slack: ask"})
    assert settings.summary_thinking is False and settings.proposer_thinking is False
    updated = settings.with_overrides({"proposerIntervalS": 60, "summaryThinking": True, "permissionsApps": "Notes: Use,", "permissionsDefault": "look",
                                       "proposerModel": "big-model", "proposerApiKey": "k", "summaryBaseUrl": ""})
    assert updated.proposer_interval_s == 60 and updated.summary_thinking and updated.permissions_apps == {"Notes": "use"}
    assert updated.permissions == Permissions(Level.LOOK, {"Notes": Level.USE}, {"chase.com": Level.BLOCK})
    assert updated.proposer == ModelEndpoint(model="big-model", base_url="https://api.openai.com/v1", api_key="k", provider="openai", stream=False)
    assert updated.summary.base_url == "http://vllm/v1"
    assert updated.with_overrides({"proposerStream": True}).proposer.stream is True
    tuned = updated.with_overrides({"proposerDedupThreshold": 0.9, "proposerDedupSessionHours": 6, "embedApiKey": "ek", "proposerDedup": False})
    assert (tuned.proposer_dedup, tuned.proposer_dedup_threshold, tuned.proposer_dedup_session_hours, tuned.embed.api_key) == (False, 0.9, 6.0, "ek")
    assert updated.with_overrides({"summaryProvider": "OpenAI"}).summary.provider == "openai"
    for bad in ({"snapshotIntervalS": 1}, {"proposerThinking": "yes"}, {"unknown": 1}, {"summaryModel": 5}, {"summaryProvider": "azure"}, {"beliefStream": "yes"}, {"proposerDedupThreshold": 1.5}, {"proposerDedupNotNowHours": 0},
                {"maxProposals": 0}, {"maxProposals": 6}, {"proposerMaxTokens": -1}, {"permissionsDefault": "read"}, {"permissionsApps": "Slack"},
                {"permissionsSites": ["chase.com"]}):
        with pytest.raises(ValueError):
            settings.with_overrides(bad)
    wire = updated.wire()
    assert "proposerApiKey" not in wire and wire["proposerModel"] == "big-model" and wire["summaryModel"] == "qwen"
    assert wire["proposerProvider"] == "openai" and wire["summaryProvider"] == "vllm" and wire["beliefProvider"] == "vllm"
    with pytest.raises(ValueError, match="provider must be one of vllm, openai"):
        saved(tmp_path, "summary: {provider: azure}\n")
    assert settings.max_proposals == 3 and settings.with_overrides({"maxProposals": "5"}).max_proposals == 5
    assert settings.proposer_max_tokens == 0 and settings.with_overrides({"proposerMaxTokens": 4096}).proposer_max_tokens == 4096
    from_file = saved(tmp_path, """
        summary: {max_tokens: 1200}
        proposer: {context_summaries: 7}
        observer: {max_elements: 250, screenshot_max_side: 800}
    """)
    assert from_file.summary_max_tokens == 1200 and wire["summaryMaxTokens"] == 0
    assert (from_file.max_elements, from_file.proposer_context_summaries, from_file.screenshot_max_side) == (250, 7, 800) and settings.proposer_context_summaries == 20
    assert settings.screenshot_mode == "switch" and settings.effective_screenshot_mode == "switch"
    assert settings.max_session_minutes == 120 and wire["maxSessionMinutes"] == 120
    visual = saved(tmp_path, """
        summary: {screenshots: true}
        observer: {screenshot_mode: off}
        session: {max_session_minutes: 45}
    """)
    assert visual.summary_screenshots and visual.screenshot_mode == "off" and visual.effective_screenshot_mode == "always"  # off stays a word (YAML 1.2)
    assert visual.max_session_minutes == 45 and visual.wire()["effectiveScreenshotMode"] == "always"
    limitless = visual.with_overrides({"maxSessionMinutes": 0, "screenshotMode": "always", "summaryScreenshots": False})
    assert limitless.max_session_minutes == 0 and limitless.effective_screenshot_mode == "always"
    for bad in ({"screenshotMode": "sometimes"}, {"maxSessionMinutes": -1}, {"screenshotMaxSide": 0}):
        with pytest.raises(ValueError):
            visual.with_overrides(bad)


def test_proposer_inputs_setting(tmp_path):
    assert LiveSettings.load().proposer_inputs == ("tasks", "worklog", "snapshots", "current_window", "feedback", "chat", "preferences")  # no file at all
    windows = saved(tmp_path, "proposer: {feedback_window: {rejected: 5, accepted: 2, recent: 1}}\n")
    assert (windows.proposer_feedback_rejected, windows.proposer_feedback_accepted, windows.proposer_feedback_recent) == (5, 2, 1)
    assert windows.with_overrides({"proposerFeedbackAccepted": 4}).wire()["proposerFeedbackAccepted"] == 4
    assert saved(tmp_path, "proposer: {feedback_window: 6}\n").proposer_feedback_rejected == 6  # an older file's one number: the defaults
    for bad in ({"proposerFeedbackRejected": 0}, {"proposerFeedbackRecent": -1}):
        with pytest.raises(ValueError):
            windows.with_overrides(bad)
    settings = saved(tmp_path, "proposer: {inputs: 'current_window, Worklog'}\n")
    assert settings.proposer_inputs == ("worklog", "current_window")  # canonical order, case-insensitive
    assert settings.wire()["proposerInputs"] == ["worklog", "current_window"]
    assert settings.with_overrides({"proposerInputs": ["snapshots"]}).proposer_inputs == ("snapshots",)
    assert settings.with_overrides({"proposerInputs": "snapshots,worklog"}).proposer_inputs == ("worklog", "snapshots")
    for bad in ({"proposerInputs": "everything"}, {"proposerInputs": True}, {"proposerInputs": []},
                {"proposerInputs": [1]}):
        with pytest.raises(ValueError, match="proposerInputs"):
            settings.with_overrides(bad)
    with pytest.raises(ValueError, match="proposerInputs"):
        saved(tmp_path, "proposer: {inputs: all}\n")


def test_belief_settings(tmp_path):
    settings = saved(tmp_path, "belief: {interval_s: 120}\n")
    assert (settings.belief_interval_s, settings.belief_thinking, settings.belief_stale_days) == (120, True, 7)
    assert settings.belief_done_step_days == 21 and settings.belief_stale_days == 7 and settings.with_overrides({"beliefDoneStepDays": 14}).wire()["beliefDoneStepDays"] == 14
    assert settings.actor_yield_s == 20 and settings.with_overrides({"actorYieldS": 5}).wire()["actorYieldS"] == 5
    assert settings.actor_context_summaries == 12 and settings.with_overrides({"actorContextSummaries": 3}).wire()["actorContextSummaries"] == 3
    with pytest.raises(ValueError, match="actorContextSummaries"):
        settings.with_overrides({"actorContextSummaries": 0})
    assert saved(tmp_path, "actor: {yield_s: 45}\n").actor_yield_s == 45
    with pytest.raises(ValueError, match="actorYieldS"):
        settings.with_overrides({"actorYieldS": 0})
    assert settings.belief == ModelEndpoint(model="", base_url="http://localhost:8000/v1") and settings.wire()["beliefModel"] == ""
    with pytest.raises(ValueError, match="belief model is not set"):  # required, like the other roles: no fallback
        settings.with_overrides({"summaryModel": "s", "proposerModel": "p"}).require_models()
    assert saved(tmp_path, "belief: {model: qwen-small}\n").belief == ModelEndpoint(model="qwen-small", base_url="http://localhost:8000/v1")
    overridden = settings.with_overrides({"beliefModel": "x", "beliefBaseUrl": "http://b/v1", "beliefApiKey": "k"})
    assert overridden.belief == ModelEndpoint(model="x", base_url="http://b/v1", api_key="k") and "beliefApiKey" not in overridden.wire()
    changed = settings.with_overrides({"beliefStaleDays": 7, "beliefThinking": False, "beliefMaxTokens": 2000})
    assert (changed.belief_stale_days, changed.belief_thinking, changed.belief_max_tokens) == (7, False, 2000)
    assert saved(tmp_path, "belief: {trash_days: 3}\n").belief_trash_days == 3 and settings.with_overrides({"beliefTrashDays": 2}).wire()["beliefTrashDays"] == 2
    for bad in ({"beliefIntervalS": 1}, {"beliefStaleDays": -1}, {"beliefTrashDays": 0}):
        with pytest.raises(ValueError):
            settings.with_overrides(bad)


def test_preference_timer_settings(tmp_path):
    settings = saved(tmp_path, "preference: {interval_s: 120, min_feedback: 3}\n")
    assert (settings.preference_interval_s, settings.preference_min_feedback) == (120, 3) and saved(tmp_path, "").preference_min_feedback == 1
    assert settings.with_overrides({"preferenceIntervalS": 600, "preferenceMinFeedback": 1}).wire()["preferenceIntervalS"] == 600
    for bad in ({"preferenceIntervalS": 1}, {"preferenceMinFeedback": 0}):
        with pytest.raises(ValueError):
            settings.with_overrides(bad)


def test_remote_desktop_settings(monkeypatch, tmp_path):
    settings = fake_settings()
    assert settings.remote_desktop_provider is None and settings.wire()["desktopRemoteProvider"] is None
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_TOKEN", "s3cret")  # the secret stays in the environment (.env), never in settings.yaml
    from_file = saved(tmp_path, "remote_desktop: {provider: cua, url: wss://desk.example.org}\n")
    assert (from_file.remote_desktop_provider, from_file.remote_desktop_url, from_file.remote_desktop_token) == ("cua", "wss://desk.example.org", "s3cret")
    wire = from_file.wire()
    assert wire["desktopRemoteProvider"] == "cua" and wire["desktopRemoteUrl"] == "wss://desk.example.org" and "s3cret" not in str(wire)
    assert wire["desktopRemoteTokenHint"] == "•••" and settings.wire()["desktopRemoteTokenHint"] == ""  # a short secret shows nothing of itself
    assert settings.with_overrides({"desktopRemoteToken": " t0k "}).remote_desktop_token == "t0k"  # the page's, to .env on a save
    changed = settings.with_overrides({"desktopRemoteProvider": "CUA", "desktopRemoteUrl": " ws://h:1 "})
    assert (changed.remote_desktop_provider, changed.remote_desktop_url) == ("cua", "ws://h:1")
    with pytest.raises(ValueError, match="desktopRemoteProvider must be one of cua"):
        settings.with_overrides({"desktopRemoteProvider": "vmware"})
    with pytest.raises(ValueError, match="desktopRemoteProvider"):
        saved(tmp_path, "remote_desktop: {provider: vmware}\n")
    assert settings.actor_desktop_source == ("local",) and settings.wire()["actorDesktopSource"] == ["local"]
    assert saved(tmp_path, "actor: {desktop_source: 'remote, local'}\n").actor_desktop_source == ("local", "remote")  # canonical order
    both = settings.with_overrides({"actorDesktopSource": ["remote"]})
    assert both.actor_desktop_source == ("remote",)
    with pytest.raises(ValueError, match="includes remote"):
        both.require_desktops()  # no provider configured on fake settings
    from_file.with_overrides({"actorDesktopSource": "local,remote"}).require_desktops()  # provider, url and token set above
    for bad in ({"actorDesktopSource": "cloud"}, {"actorDesktopSource": []}):
        with pytest.raises(ValueError, match="actorDesktopSource"):
            settings.with_overrides(bad)
    # The agents a session sets up, and the cmux agent's Claude Code options.
    assert settings.actor_agents == ("desktop",) and settings.wire()["actorAgents"] == ["desktop"] and settings.wire()["actorCmuxModel"] is None  # the helper's
    assert LiveSettings.__dataclass_fields__["actor_agents"].default == ("claude",)  # the real default: Claude Code
    assert settings.wire()["actorCmuxPermissionMode"] == "auto" and LiveSettings.load().actor_cmux_permission_mode == "auto"  # the default
    agents = saved(tmp_path, "actor: {agents: 'cmux, desktop', cmux_model: sonnet, cmux_permission_mode: dontAsk, cmux_cwd: ~/work}\n")
    assert agents.actor_agents == ("desktop", "cmux") and (agents.actor_cmux_model, agents.actor_cmux_permission_mode, agents.actor_cmux_cwd) == ("sonnet", "dontAsk", "~/work")
    codex = settings.with_overrides({"actorAgents": "claude,codex", "actorCodexModel": " gpt-5.6-terra ", "actorCodexSandbox": "read-only", "actorCodexCwd": "/tmp"})
    assert codex.actor_agents == ("claude", "codex") and (codex.actor_codex_model, codex.actor_codex_sandbox, codex.actor_codex_cwd) == ("gpt-5.6-terra", "read-only", "/tmp")
    assert settings.actor_codex_sandbox == "workspace-write" and codex.wire()["actorCodexSandbox"] == "read-only"  # the default, and as the console reads it
    only = settings.with_overrides({"actorAgents": "cmux", "actorCmuxModel": " opus ", "actorCmuxPermissionMode": "acceptEdits", "actorCmuxCwd": "/tmp"})
    assert only.wire()["actorAgents"] == ["cmux"] and (only.actor_cmux_model, only.actor_cmux_permission_mode, only.actor_cmux_cwd) == ("opus", "acceptEdits", "/tmp")
    for bad, name in (({"actorAgents": "gemini"}, "actorAgents"), ({"actorAgents": []}, "actorAgents"), ({"actorCmuxPermissionMode": "yolo"}, "actorCmuxPermissionMode"),
                      ({"actorCodexSandbox": "anything-goes"}, "actorCodexSandbox")):
        with pytest.raises(ValueError, match=name):
            settings.with_overrides(bad)


def test_a_role_s_endpoint_comes_from_its_provider_s_key_set_unless_the_file_gives_its_own_address(monkeypatch, tmp_path):
    """The most specific place wins: the role's own base_url in settings.yaml, then its provider's key set in .env (its base
    URL and its key), then the provider's own base URL. A key is only ever a provider's (or the form's)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    monkeypatch.setenv("LITELLM_API_KEY", "ll-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "")  # set but empty, as an example file leaves it: the provider's own URL
    settings = saved(tmp_path, """
        summary: {provider: openrouter, model: summary-model}
        belief: {provider: anthropic, model: belief-model}
        proposer: {provider: openai, model: proposer-model}
        embed: {provider: litellm, model: embed-model}
    """)
    assert (settings.summary.base_url, settings.summary.api_key) == ("https://openrouter.ai/api/v1", "or-key")
    assert (settings.belief.base_url, settings.belief.api_key) == ("https://api.anthropic.com/v1/", "an-key")
    assert (settings.proposer.base_url, settings.proposer.api_key) == ("https://proxy.example/v1", "oa-key")  # the key set's own base URL
    assert (settings.embed.base_url, settings.embed.api_key) == ("http://localhost:4000", "ll-key")
    own = saved(tmp_path, "summary: {provider: openrouter, model: m, base_url: http://vllm/v1}\n")  # the role's own address wins over its provider's
    assert (own.summary.base_url, own.summary.api_key) == ("http://vllm/v1", "or-key")
    # Another provider picked on the form: its key set, unless the form gives the endpoint.
    switched = settings.with_overrides({"proposerProvider": "openrouter"})
    assert (switched.proposer.provider, switched.proposer.base_url, switched.proposer.api_key) == ("openrouter", "https://openrouter.ai/api/v1", "or-key")
    pinned = settings.with_overrides({"proposerProvider": "anthropic", "proposerBaseUrl": "https://litellm.example/v1", "proposerApiKey": "lk"})
    assert (pinned.proposer.base_url, pinned.proposer.api_key) == ("https://litellm.example/v1", "lk")


def test_a_vllm_role_reads_the_vllm_key_set_else_vllm_serve_s_own_address(monkeypatch, tmp_path):
    assert saved(tmp_path, "summary: {model: qwen}\n").summary.base_url == "http://localhost:8000/v1"  # vllm is the default provider
    monkeypatch.setenv("VLLM_BASE_URL", "http://gpu:8000/v1")
    settings = LiveSettings.load()
    assert (settings.summary.base_url, settings.summary.api_key, settings.wire()["vllmBaseUrl"]) == ("http://gpu:8000/v1", None, "http://gpu:8000/v1")
    given = settings.with_overrides({"vllmBaseUrl": "http://other/v1", "vllmApiKey": "v-key"})  # the form's, for every vllm role
    assert (given.summary.base_url, given.summary.api_key, given.belief.base_url) == ("http://other/v1", "v-key", "http://other/v1")
    with pytest.raises(ValueError, match="vllmBaseUrl"):
        settings.with_overrides({"vllmBaseUrl": 3})


def test_save_writes_each_setting_where_load_reads_it(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "SHELL", frozenset())
    monkeypatch.setenv("OPENROUTER_API_KEY", "")  # restored afterwards, whatever .env sets
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_TOKEN", "")
    settings = LiveSettings.load()
    # Every setting the page shows has a place: in settings.yaml, or a provider's in .env; the rest of the wire view is computed.
    assert set(PLACES) <= set(settings.wire()) and set(settings.wire()) - set(PLACES) - set(PROVIDER_FIELDS) == {"effectiveScreenshotMode", "providerKeys", "desktopRemoteTokenHint"}
    given = {"proposerThinking": False, "actorAgents": ["claude", "codex"], "maxProposals": 4, "proposerDedupThreshold": 0.5,
             "permissionsApps": "Safari: look, Notes: use", "summaryProvider": "openrouter", "summaryModel": "m", "openrouterApiKey": "or-key",
             "desktopRemoteUrl": None, "desktopRemoteToken": "t0k", "snapshotIntervalS": 12, "screenshotMode": "off"}
    settings.save(given)
    written = YAML(typ="safe").load((tmp_path / "settings.yaml").read_text())
    assert written["proposer"] == {"thinking": False, "max_proposals": 4, "dedup_threshold": 0.5} and written["actor"] == {"agents": ["claude", "codex"]}
    assert written["observer"] == {"snapshot_interval_s": 12.0, "screenshot_mode": "off"} and written["permissions"] == {"apps": {"Safari": "look", "Notes": "use"}}
    assert written["summary"] == {"provider": "openrouter", "model": "m"} and written["remote_desktop"] == {"url": None}  # a blank one reads as unset
    assert "url: null" in (tmp_path / "settings.yaml").read_text()  # written as the word, not left blank
    assert "or-key" not in (tmp_path / "settings.yaml").read_text() and "t0k" not in (tmp_path / "settings.yaml").read_text()  # a secret goes to .env only
    assert dotenv_values(tmp_path / ".env") == {"OPENROUTER_API_KEY": "or-key", "CUA_DESKTOP_REMOTE_TOKEN": "t0k"}
    expected, back = settings.with_overrides(given).wire(), LiveSettings.load().wire()
    assert all(back[key] == expected[key] for key in given if key in back and given[key] is not None)
    with pytest.raises(ValueError, match="maxProposals"):
        settings.save({"maxProposals": 99})


async def test_the_settings_page_saves_to_the_files_and_reads_a_hand_edit_back(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "SHELL", frozenset())
    monkeypatch.setenv("OPENAI_API_KEY", "")  # restored afterwards, whatever .env sets
    monkeypatch.setenv("CUA_DESKTOP_REMOTE_TOKEN", "")
    (tmp_path / "settings.yaml").write_text("proposer:\n  model: old\n")
    live, _client, _ = make_manager(tmp_path / "data")
    app = create_app(manager=RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[]), live=live, acquire_lease=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        saved = await http.post("/api/live/settings", json={"proposerModel": "new", "summaryModel": "s", "openaiApiKey": "sk-proj-0123456789abcdef", "desktopRemoteToken": "t0k"})
        assert saved.status_code == 200 and saved.json()["defaults"]["proposerModel"] == "new" and "sk-proj-0123456789abcdef" not in saved.text and "t0k" not in saved.text
        assert (tmp_path / "settings.yaml").read_text() == "proposer:\n  model: new\nsummary:\n  model: s\n"
        assert (tmp_path / ".env").read_text() == "export OPENAI_API_KEY='sk-proj-0123456789abcdef'\nexport CUA_DESKTOP_REMOTE_TOKEN='t0k'\n"
        assert saved.json()["defaults"]["providerKeys"]["openai"] == "sk-pro…cdef" and saved.json()["defaults"]["desktopRemoteTokenHint"] == "•••"  # abbreviated
        (tmp_path / "settings.yaml").write_text("summary:\n  model: by-hand  # mine\n")
        assert (await http.post("/api/live/settings")).json()["defaults"]["summaryModel"] == "by-hand"
        assert (tmp_path / "settings.yaml").read_text() == "summary:\n  model: by-hand  # mine\n"  # only read: a file edited by hand is left as it is
        assert (await http.post("/api/live/settings", json={"summaryModel": "s2"})).json()["defaults"]["summaryModel"] == "s2"
        assert (tmp_path / "settings.yaml").read_text().startswith("summary:\n  model: s2 ") and "# mine" in (tmp_path / "settings.yaml").read_text()  # a save keeps the comments
        assert (await http.post("/api/live/settings", json={"maxProposals": 99})).status_code == 400


def test_a_provider_s_key_given_on_the_form_serves_every_role_on_it_without_a_key_of_its_own(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    settings = saved(tmp_path, "".join(f"{role}: {{provider: openrouter, model: m}}\n" for role in ("summary", "belief", "proposer", "embed")))
    assert settings.wire()["providerKeys"] == {"openai": "", "anthropic": "", "openrouter": "•••", "litellm": "", "vllm": ""}  # where the backend has keys, abbreviated
    given = settings.with_overrides({"openrouterApiKey": "form-key", "proposerApiKey": "proposer-own"})
    assert (given.summary.api_key, given.belief.api_key, given.proposer.api_key, given.embed.api_key) == ("form-key", "form-key", "proposer-own", "form-key")
    assert "form-key" not in str(given.wire()) and given.summary.base_url == "https://openrouter.ai/api/v1"
    moved = settings.with_overrides({"embedProvider": "openai", "openaiApiKey": "oa-form"})  # the embedder on another provider, with its key
    assert (moved.embed.provider, moved.embed.base_url, moved.embed.api_key) == ("openai", "https://api.openai.com/v1", "oa-form")
    assert moved.wire()["embedProvider"] == "openai" and moved.summary.api_key == "env-key"  # the other roles keep theirs
    with pytest.raises(ValueError, match="openaiApiKey"):
        settings.with_overrides({"openaiApiKey": 3})


def test_belief_context_and_lookalike_settings(tmp_path):
    belief = saved(tmp_path, "belief: {context_summaries: 10, similar_threshold: 0}\n")
    assert (belief.belief_context_summaries, belief.belief_similar_threshold) == (10, 0.0) and belief.wire()["beliefSimilarThreshold"] == 0.0
    assert belief.with_overrides({"beliefContextSummaries": 0}).belief_context_summaries == 0  # none shown
    for bad in ({"beliefContextSummaries": -1}, {"beliefSimilarThreshold": 2}):
        with pytest.raises(ValueError):
            belief.with_overrides(bad)
