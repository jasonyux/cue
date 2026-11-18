"""Configuration check: every model endpoint answers a one-word probe, and the observer has its permissions."""

from unittest.mock import AsyncMock

import httpx
import pytest
from live_helpers import FakeClient, fake_settings, make_manager

from app.api import create_app
from app.live.config import LiveSettings, ModelEndpoint
from app.live.health import check_settings
from app.runner import RunnerManager


async def _ok_agent(default_model):
    return {"role": "agent", "label": "Task agent", "ok": True, "model": default_model}


@pytest.mark.parametrize("flags", [(False, True, True, False), (True, False, False, True)])
async def test_health_respects_each_roles_reasoning_requirement(monkeypatch, flags):
    """A valid live configuration must also pass against reasoning-required endpoints."""
    made = []

    class ReasoningEndpoint(FakeClient):
        def __init__(self, expected, tool):
            super().__init__([[('ping', {'ok': True})]] if tool else ["OK"])
            self.expected = expected

        async def complete(self, messages, **kwargs):
            if kwargs["thinking"] != self.expected:
                raise RuntimeError("Reasoning setting is incompatible with this endpoint")
            if self.expected and kwargs["max_tokens"] < 1024:
                raise RuntimeError("No room for reasoning before the response")
            return await super().complete(messages, **kwargs)

    def factory(endpoint, **kwargs):
        index = len(made)
        client = ReasoningEndpoint(flags[index], tool=index >= 1)  # all but the summarizer (the proposer searches history)
        made.append(client)
        return client

    monkeypatch.setattr("app.live.health.price_for", lambda model: {"input": 0.0, "output": 0.0})
    result = await check_settings(
        fake_settings(summary_thinking=flags[0], proposer_thinking=flags[1], belief_thinking=flags[2], preference_thinking=flags[3]),
        default_model="m", client_factory=factory, agent=_ok_agent,
        observer=lambda: {"role": "observer", "ok": True},
    )
    assert result["ok"], result["checks"]
    assert len(made) == len(flags)
    assert all(client.closed for client in made)


async def test_the_proposer_is_probed_with_a_tool_when_it_searches_history():
    """Live, a proposer with search rounds is given the search tool every round; an endpoint that refuses tools (gpt-6 with
    reasoning on Chat Completions) must fail the check, not the session."""
    made = []

    def factory(endpoint, **kwargs):
        made.append(FakeClient(["OK"]))
        return made[-1]

    common = {"default_model": "m", "client_factory": factory, "agent": _ok_agent, "observer": lambda: {"role": "observer", "ok": True}}
    searching = await check_settings(fake_settings(), **common)
    proposer = searching["checks"][1]
    assert proposer["label"] == "Proposer (tool calls)" and not proposer["ok"] and "no tool call" in proposer["error"]
    assert made[1].calls[0]["tools"] == ["ping"]
    plain = await check_settings(fake_settings(proposer_search_rounds=0), **common)
    assert plain["checks"][1]["label"] == "Proposer" and plain["checks"][1]["ok"] and made[5].calls[0]["tools"] == []


async def test_check_settings_reports_each_endpoint(monkeypatch):
    settings = fake_settings().with_overrides({"proposerModel": "broken-model"})
    unset = await check_settings(LiveSettings(summary=ModelEndpoint(), belief=ModelEndpoint(), proposer=settings.proposer), default_model="m",
                                 client_factory=lambda *a, **k: FakeClient(["OK"]), agent=_ok_agent,
                                 observer=lambda: {"role": "observer", "label": "Observer", "ok": True})
    assert unset["checks"][0]["ok"] is False and "summary.model" in unset["checks"][0]["error"]
    made = []

    def factory(endpoint, *, timeout):
        made.append((endpoint.model, timeout))
        if endpoint.model == "broken-model":
            raise RuntimeError("no such model")
        return FakeClient(["OK"])

    async def agent(default_model):
        return {"role": "agent", "label": "Task agent", "ok": True, "model": default_model, "latencyMs": 3, "detail": "OK"}

    result = await check_settings(settings, default_model="agent-model", client_factory=factory, agent=agent,
                                  observer=lambda: {"role": "observer", "label": "Observer", "ok": False, "error": "RuntimeError: no permission"})
    assert result["ok"] is False and [item["role"] for item in result["checks"]] == ["summary", "proposer", "belief", "preference", "agent", "observer", "prices"]
    summary, proposer, belief, preference, agent_check, observer, prices = result["checks"]
    assert not preference["ok"] and "no tool call" in preference["error"] and preference["label"] == "Preference updater (tool calls)"  # probed like the belief updater
    assert not prices["ok"] and prices["error"].startswith("No price for fake-model, broken-model, agent-model:")  # none is in the table
    assert summary["ok"] and summary["detail"] == "OK" and summary["model"] == "fake-model" and summary["latencyMs"] >= 0
    assert not proposer["ok"] and proposer["error"] == "RuntimeError: no such model" and proposer["model"] == "broken-model"
    assert agent_check["ok"] and agent_check["model"] == "agent-model" and observer["error"].endswith("no permission")
    assert not belief["ok"] and "no tool call" in belief["error"]  # the text-only fake never calls the ping tool
    assert made == [("fake-model", 30.0), ("broken-model", 30.0), ("fake-model", 30.0), ("fake-model", 30.0)]
    monkeypatch.setattr("app.live.health.price_for", lambda model: {"input": 0.0, "output": 0.0} if model == "fake-model" else {"input": 2.0, "output": 10.0})
    priced = await check_settings(fake_settings(), default_model="gpt-6-sol", client_factory=factory, agent=agent,
                                  observer=lambda: {"role": "observer", "label": "Observer", "ok": True})
    assert priced["checks"][-1] == {"role": "prices", "label": "Model prices", "ok": True, "detail": "per 1M tokens in/out: fake-model $0/$0 · gpt-6-sol $2/$10"}


async def test_settings_check_route_validates_and_probes(monkeypatch, tmp_path):
    live, _client, _ = make_manager(tmp_path / "data")
    runner = RunnerManager(tmp_path / "data", executor=AsyncMock(), scenarios=[])
    app = create_app(manager=runner, live=live, acquire_lease=False)
    seen = {}

    async def fake_check(settings, *, default_model):
        seen["model"] = settings.proposer.model
        seen["default"] = default_model
        return {"ok": True, "checks": []}

    monkeypatch.setattr("app.live.api.check_settings", fake_check)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://runner") as http:
        assert (await http.post("/api/live/settings/check", json={"bogus": 1})).status_code == 400
        checked = await http.post("/api/live/settings/check", json={"proposerModel": "other"})
        assert checked.status_code == 200 and checked.json() == {"ok": True, "checks": []}
        assert seen["model"] == "other" and seen["default"]


async def test_check_settings_probes_the_remote_desktop_when_configured():
    probes = []

    async def remote(provider, url, token):
        probes.append((provider, url, token))
        return {"role": "remote", "label": "Remote desktop", "ok": True, "model": provider, "baseUrl": url}

    common = {"default_model": "m", "client_factory": lambda *a, **k: FakeClient(["OK"]), "agent": _ok_agent,
              "observer": lambda: {"role": "observer", "label": "Observer", "ok": True}, "remote": remote}
    plain = await check_settings(fake_settings(), **common)
    assert [item["role"] for item in plain["checks"]] == ["summary", "proposer", "belief", "preference", "agent", "observer", "prices"] and probes == []
    configured = fake_settings(actor_desktop_source=("local", "remote"), remote_desktop_provider="cua", remote_desktop_url="wss://desk",
                               remote_desktop_token="t")
    result = await check_settings(configured, **common)
    assert [item["role"] for item in result["checks"]][-2:] == ["remote", "prices"] and probes == [("cua", "wss://desk", "t")]
    unused = await check_settings(fake_settings(remote_desktop_provider="cua", remote_desktop_url="wss://desk", remote_desktop_token="t"), **common)
    assert [item["role"] for item in unused["checks"]][-2:] == ["observer", "prices"] and len(probes) == 1  # configured but not in the source
    half = await check_settings(fake_settings(actor_desktop_source=("remote",), remote_desktop_provider="cua", remote_desktop_url="wss://desk"), **common)
    assert half["checks"][-2]["ok"] is False and "CUA_DESKTOP_REMOTE_TOKEN" in half["checks"][-2]["error"] and len(probes) == 2


async def test_check_settings_probes_the_embeddings_endpoint_when_the_duplicate_filter_is_on():
    class FakeEmbedder:
        def __init__(self, endpoint, *, timeout):
            self.endpoint, self.timeout = endpoint, timeout

        async def embed(self, texts):
            if self.endpoint.model == "dead-embed":
                raise RuntimeError("connection refused")
            return [[0.1, 0.2, 0.3]] * len(texts)

        async def close(self):
            pass

    common = {"default_model": "m", "client_factory": lambda *a, **k: FakeClient(["OK"]), "agent": _ok_agent,
              "observer": lambda: {"role": "observer", "label": "Observer", "ok": True}, "embedder_factory": FakeEmbedder}
    off = await check_settings(fake_settings(), **common)
    assert "embed" not in [item["role"] for item in off["checks"]]
    on = await check_settings(fake_settings(proposer_dedup=True, embed=ModelEndpoint(model="embed-1", base_url="http://e/v1")), **common)
    probe = next(item for item in on["checks"] if item["role"] == "embed")
    assert probe["ok"] and probe["model"] == "embed-1" and probe["baseUrl"] == "http://e/v1" and probe["detail"] == "3 dimensions"
    assert [item["role"] for item in on["checks"]] == ["summary", "proposer", "belief", "preference", "embed", "agent", "observer", "prices"]
    dead = await check_settings(fake_settings(proposer_dedup=True, embed=ModelEndpoint(model="dead-embed", base_url="http://dead/v1")), **common)
    assert not dead["ok"] and next(item for item in dead["checks"] if item["role"] == "embed")["error"] == "RuntimeError: connection refused"
    unset = await check_settings(fake_settings(proposer_dedup=True), **common)
    assert "embed.model" in next(item for item in unset["checks"] if item["role"] == "embed")["error"]
