"""Check a session's configuration before starting it.

Each model endpoint (summarizer, proposer, and the task agent) gets a one-word completion with a short timeout, the
belief updater's endpoint is probed for parsed tool calls, the observer's macOS permissions are checked, and every
configured model must have a price in the price table, so a wrong URL, key, or model name shows up here instead of as
a silent stream of failed ticks, and a session never runs on a model whose cost nobody can add up.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..responses_loop import create_default_responses_client
from ..usage import price_for
from .actor.cmux.cli import cmux
from .actor.desktop.remote import connect as connect_remote_desktop
from .actor.standalone.claude import cli as claude_cli
from .actor.standalone.codex import AsyncCodex
from .config import LiveSettings, ModelEndpoint
from .utils.embed import EmbeddingClient
from .utils.llm import LiveModelClient

PROBE_PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT_S = 30.0
PROBE_THINKING_TOKENS = 4096  # Include room for reasoning before the short answer/tool call.


def failure(role: str, label: str, error: BaseException, **fields: Any) -> dict[str, Any]:
    return {"role": role, "label": label, "ok": False, "error": f"{type(error).__name__}: {str(error)[:300]}", **fields}


async def probe_chat(role: str, label: str, endpoint: ModelEndpoint, client_factory: Callable[..., Any], *, thinking: bool = False) -> dict[str, Any]:
    """One tiny Chat Completions call through the same client a live loop uses."""
    fields = {"model": endpoint.model, "baseUrl": endpoint.base_url or os.environ.get("OPENAI_BASE_URL")}
    if not endpoint.model:
        return {"role": role, "label": label, "ok": False, **fields,
                "error": f"Not configured: set {role}.model in settings.yaml or fill the model on the form (no fallback to the task agent's model)."}
    started = time.monotonic()
    try:
        client = client_factory(endpoint, timeout=PROBE_TIMEOUT_S)
    except Exception as error:  # noqa: BLE001 - a bad key resolves at construction
        return failure(role, label, error, **fields)
    try:
        reply = await asyncio.wait_for(client.complete([{"role": "user", "content": PROBE_PROMPT}], thinking=thinking,
                                                       max_tokens=PROBE_THINKING_TOKENS if thinking else 16),
                                       PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001 - every failure is a result, not a crash
        return failure(role, label, error, **fields)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()
    return {"role": role, "label": label, "ok": True, **fields, "latencyMs": round((time.monotonic() - started) * 1000),
            "detail": reply.content.strip()[:80] or "(empty reply)"}


async def probe_agent(default_model: str) -> dict[str, Any]:
    """One tiny Responses API call the way the task agent makes them."""
    fields = {"model": default_model, "baseUrl": os.environ.get("OPENAI_BASE_URL")}
    started = time.monotonic()
    try:
        client = create_default_responses_client()
        response = await asyncio.wait_for(
            client.create({"model": default_model, "input": PROBE_PROMPT, "max_output_tokens": 16}), PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001
        return failure("agent", "Task agent", error, **fields)
    text = ""
    for item in response.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                text = str(part.get("text") or "")
    return {"role": "agent", "label": "Task agent", "ok": True, **fields,
            "latencyMs": round((time.monotonic() - started) * 1000), "detail": text.strip()[:80] or "(responded)"}


def probe_observer() -> dict[str, Any]:
    """The recorder needs Accessibility and Screen Recording permission for the app that launched the backend."""
    if sys.platform != "darwin":
        return {"role": "observer", "label": "Observer", "ok": False, "error": "The accessibility observer needs macOS."}
    try:
        from ..desktop.ax import AccessibilityDesktop

        AccessibilityDesktop.check_permissions()
    except Exception as error:  # noqa: BLE001
        return failure("observer", "Observer", error)
    return {"role": "observer", "label": "Observer", "ok": True, "detail": "Accessibility and Screen Recording granted"}


async def probe_embeddings(endpoint: ModelEndpoint, embedder_factory: Callable[..., Any]) -> dict[str, Any]:
    """One tiny embeddings call for the proposer's duplicate filter, through a client with no cache file."""
    fields = {"model": endpoint.model, "baseUrl": endpoint.base_url or os.environ.get("OPENAI_BASE_URL")}
    if not endpoint.model:
        return {"role": "embed", "label": "Duplicate filter (embeddings)", "ok": False, **fields,
                "error": "Not configured: the duplicate filter is on; set embed.model in settings.yaml (with its provider) or fill it on the form."}
    started = time.monotonic()
    try:
        client = embedder_factory(endpoint, timeout=PROBE_TIMEOUT_S)
    except Exception as error:  # noqa: BLE001 - a bad key resolves at construction
        return failure("embed", "Duplicate filter (embeddings)", error, **fields)
    try:
        vectors = await asyncio.wait_for(client.embed([PROBE_PROMPT]), PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001
        return failure("embed", "Duplicate filter (embeddings)", error, **fields)
    finally:
        await client.close()
    return {"role": "embed", "label": "Duplicate filter (embeddings)", "ok": True, **fields,
            "latencyMs": round((time.monotonic() - started) * 1000), "detail": f"{len(vectors[0])} dimensions"}


PROBE_TOOL = {"type": "function", "function": {"name": "ping", "description": "Reply to a ping.",
                                              "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}}}


async def probe_tools(role: str, label: str, endpoint: ModelEndpoint, client_factory: Callable[..., Any], *, thinking: bool = False) -> dict[str, Any]:
    """A role that calls tools (the belief and preference updaters, a proposer that searches history) needs them to come
    back parsed, with its thinking setting: on vLLM that is a server flag, and some APIs refuse tools with reasoning."""
    fields = {"model": endpoint.model, "baseUrl": endpoint.base_url or os.environ.get("OPENAI_BASE_URL")}
    if not endpoint.model:
        return {"role": role, "label": label, "ok": False, **fields,
                "error": f"Not configured: set {role}.model in settings.yaml or fill the model on the form (no fallback to another model)."}
    started = time.monotonic()
    try:
        client = client_factory(endpoint, timeout=PROBE_TIMEOUT_S)
        reply = await asyncio.wait_for(client.complete([{"role": "user", "content": "Call the ping tool with ok=true."}],
                                                       thinking=thinking, max_tokens=PROBE_THINKING_TOKENS if thinking else 64,
                                                       tools=[PROBE_TOOL]), PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001
        return failure(role, label, error, **fields)
    finally:
        close = getattr(locals().get("client"), "close", None)
        if close is not None:
            await close()
    if not any(call.get("name") == "ping" for call in reply.tool_calls):
        return {"role": role, "label": label, "ok": False, **fields,
                "error": "The endpoint returned no tool call. On vLLM start the server with --enable-auto-tool-choice "
                         "--tool-call-parser hermes (Qwen); this role needs parsed tool calls."}
    return {"role": role, "label": label, "ok": True, **fields, "latencyMs": round((time.monotonic() - started) * 1000),
            "detail": "tool call returned"}


async def probe_remote(provider: str, url: str, token: str) -> dict[str, Any]:
    """Open the remote desktop with the token and wait for its worker's ready line."""
    fields = {"model": provider, "baseUrl": url}
    started = time.monotonic()
    try:
        runtime = await connect_remote_desktop(provider, url, token, timeout_s=PROBE_TIMEOUT_S)
    except Exception as error:  # noqa: BLE001 - every failure is a result
        return failure("remote", "Remote desktop", error, **fields)
    await runtime.close()
    return {"role": "remote", "label": "Remote desktop", "ok": True, **fields, "latencyMs": round((time.monotonic() - started) * 1000),
            "detail": f"worker ready ({runtime.platform}, {runtime.mode})"}


async def probe_cmux(settings: LiveSettings) -> dict[str, Any]:
    """The cmux agent needs the backend to reach cmux's socket; Claude Code itself runs inside the workspace cmux opens."""
    fields = {"model": settings.actor_cmux_model or "Claude Code's default model"}
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(cmux("ping"), PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001 - every failure is a result
        return failure("cmux", "cmux agent (Claude Code)", error, **fields)
    return {"role": "cmux", "label": "cmux agent (Claude Code)", "ok": True, **fields,
            "latencyMs": round((time.monotonic() - started) * 1000), "detail": f"cmux answered {answer}"}


async def version(*command: str) -> str:
    """The last line ``command`` prints, or an error when it is missing or fails."""
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output = (await process.communicate())[0].decode().strip()
    if process.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {output[-200:]}")
    return output.splitlines()[-1] if output else ""


async def chromium_path() -> str:
    """Where Playwright's Chromium would be: the agent browser runs it (``pnpm python:playwright:install`` fetches it)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        return playwright.chromium.executable_path


async def browser_tools() -> str:
    """What the standalone agents' browser needs: npx for its MCP server, and Playwright's Chromium for the agent browser."""
    npx = await asyncio.wait_for(version("npx", "--version"), PROBE_TIMEOUT_S)
    chromium = await asyncio.wait_for(chromium_path(), PROBE_TIMEOUT_S)
    if not Path(chromium).exists():
        raise RuntimeError(f"The agent browser's Chromium is not installed ({chromium}): run pnpm python:playwright:install.")
    return f"npx {npx} · chromium installed"


async def probe_claude(settings: LiveSettings) -> dict[str, Any]:
    """The claude agent needs Claude Code (the one installed, else the SDK's bundled copy; signed in either way) and the
    browser's tools."""
    fields = {"model": settings.actor_claude_model or "Claude Code's default model"}
    started = time.monotonic()
    try:
        claude = await asyncio.wait_for(version(claude_cli(), "--version"), PROBE_TIMEOUT_S)
        browser = await browser_tools()
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001 - every failure is a result
        return failure("claude", "Claude Code agent", error, **fields)
    return {"role": "claude", "label": "Claude Code agent", "ok": True, **fields,
            "latencyMs": round((time.monotonic() - started) * 1000), "detail": f"claude {claude} · {browser}"}


async def codex_account() -> str:
    """Who Codex is signed in as, through the SDK and the Codex it pins; an error when nobody is."""
    async with AsyncCodex() as codex:
        answer = await codex.account()
    if answer.account is None:
        raise RuntimeError("Codex is not signed in: run codex login.")
    account = answer.account.model_dump(mode="json", by_alias=True, exclude_none=True)
    return str(account.get("email") or account.get("type") or "signed in")


async def probe_codex(settings: LiveSettings) -> dict[str, Any]:
    """The codex agent needs a Codex login (the SDK runs the Codex it pins) and the browser's tools."""
    fields = {"model": settings.actor_codex_model or "Codex's default model"}
    started = time.monotonic()
    try:
        account = await asyncio.wait_for(codex_account(), PROBE_TIMEOUT_S)
        browser = await browser_tools()
    except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001 - every failure is a result
        return failure("codex", "Codex agent", error, **fields)
    return {"role": "codex", "label": "Codex agent", "ok": True, **fields,
            "latencyMs": round((time.monotonic() - started) * 1000), "detail": f"codex signed in ({account}) · {browser}"}


def check_prices(settings: LiveSettings, default_model: str) -> dict[str, Any]:
    """Every configured model needs a price (0.0 for a self-hosted one), or the cost panel cannot add it up. The cmux and
    claude agents' model needs none: Claude Code reports its own cost; nor the codex agent's, whose cost is the price
    table's when it has one."""
    desktop_model = default_model if "desktop" in settings.actor_agents else ""
    embed_model = settings.embed.model if settings.proposer_dedup else ""
    models = [model for model in dict.fromkeys((settings.summary.model, settings.belief.model, settings.proposer.model, settings.preference.model,
                                                desktop_model, embed_model)) if model]
    prices = {model: price_for(model) for model in models}
    missing = [model for model, price in prices.items() if price is None]
    if missing:
        return {"role": "prices", "label": "Model prices", "ok": False,
                "error": f"No price for {', '.join(missing)}: add it to app/configs/model_prices.json or CUA_MODEL_PRICES "
                         "(0.0 for a self-hosted model)."}
    priced = " · ".join(f"{model} ${price['input']:g}/${price['output']:g}" for model, price in prices.items() if price is not None)
    return {"role": "prices", "label": "Model prices", "ok": True, "detail": f"per 1M tokens in/out: {priced}"}


async def check_settings(settings: LiveSettings, *, default_model: str, client_factory: Callable[..., Any] = LiveModelClient,
                         agent: Callable[[str], Awaitable[dict[str, Any]]] = probe_agent,
                         observer: Callable[[], dict[str, Any]] = probe_observer,
                         remote: Callable[[str, str, str], Awaitable[dict[str, Any]]] = probe_remote,
                         cmux_agent: Callable[[LiveSettings], Awaitable[dict[str, Any]]] = probe_cmux,
                         claude_agent: Callable[[LiveSettings], Awaitable[dict[str, Any]]] = probe_claude,
                         codex_agent: Callable[[LiveSettings], Awaitable[dict[str, Any]]] = probe_codex,
                         embedder_factory: Callable[..., Any] = lambda endpoint, **kw: EmbeddingClient(endpoint, Path(os.devnull), **kw)) -> dict[str, Any]:
    """Every check at once; ``ok`` is true only when all of them pass. Each agent is checked only when the session sets it
    up, and the remote desktop only when configured."""
    # With history search on, the proposer is given a tool every round, so it is probed with one, as it runs
    if settings.proposer_search_rounds:
        proposer = probe_tools("proposer", "Proposer (tool calls)", settings.proposer, client_factory, thinking=settings.proposer_thinking)
    else:
        proposer = probe_chat("proposer", "Proposer", settings.proposer, client_factory, thinking=settings.proposer_thinking)
    probes: list[Awaitable[dict[str, Any]]] = [
        probe_chat("summary", "Summarizer", settings.summary, client_factory, thinking=settings.summary_thinking),
        proposer,
        probe_tools("belief", "Belief updater (tool calls)", settings.belief, client_factory, thinking=settings.belief_thinking),
        probe_tools("preference", "Preference updater (tool calls)", settings.preference, client_factory, thinking=settings.preference_thinking),
    ]
    if settings.proposer_dedup:
        probes.append(probe_embeddings(settings.embed, embedder_factory))
    if "desktop" in settings.actor_agents:
        probes.append(agent(default_model))
    probes.append(asyncio.to_thread(observer))
    if "cmux" in settings.actor_agents:
        probes.append(cmux_agent(settings))
    if "claude" in settings.actor_agents:
        probes.append(claude_agent(settings))
    if "codex" in settings.actor_agents:
        probes.append(codex_agent(settings))
    wants_remote = bool({"desktop", "claude", "codex"} & set(settings.actor_agents)) and "remote" in settings.actor_desktop_source
    if wants_remote:
        probes.append(remote(settings.remote_desktop_provider or "", settings.remote_desktop_url or "", settings.remote_desktop_token or ""))
    checks = [*await asyncio.gather(*probes), check_prices(settings, default_model)]
    if wants_remote:
        try:
            settings.require_desktops()
        except ValueError as error:
            checks[-2] = {"role": "remote", "label": "Remote desktop", "ok": False, "model": settings.remote_desktop_provider or "",
                          "baseUrl": settings.remote_desktop_url, "error": f"Not configured: {error}"}
    return {"ok": all(item["ok"] for item in checks), "checks": checks}
