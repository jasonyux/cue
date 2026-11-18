"""Accumulate Responses API token usage and estimated cost per model onto the run record."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PRICES_PATH = Path(__file__).with_name("configs") / "model_prices.json"
PROFILE_PREFIXES = ("us.", "global.", "eu.", "apac.", "us-gov.")
COUNTERS = ("calls", "inputTokens", "cachedInputTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens", "totalTokens")


def model_prices() -> dict[str, dict[str, float]]:
    """Built-in price table merged with the CUA_MODEL_PRICES environment override (both USD per 1M tokens), read on each
    lookup, so a price added by hand counts without a restart."""
    prices: dict[str, dict[str, float]] = {}
    for source in (PRICES_PATH.read_text(encoding="utf-8") if PRICES_PATH.exists() else "{}", os.environ.get("CUA_MODEL_PRICES", "")):
        if not source.strip():
            continue
        try:
            table = json.loads(source)
        except json.JSONDecodeError as error:
            raise ValueError(f"CUA_MODEL_PRICES is not valid JSON: {error}") from error
        for model, price in table.items():
            if not model.startswith("_") and isinstance(price, dict):
                prices[model] = {key: float(value) for key, value in price.items()}
    return prices


def price_for(model: str) -> dict[str, float] | None:
    prices = model_prices()
    if model in prices:
        return prices[model]
    for prefix in PROFILE_PREFIXES:
        if model.startswith(prefix) and model[len(prefix):] in prices:
            return prices[model[len(prefix):]]
    return None


def estimate_cost(model: str, counts: dict[str, Any]) -> float | None:
    """Cost in USD for one model's accumulated counts, or None when no price is known."""
    price = price_for(model)
    if price is None or "input" not in price or "output" not in price:
        return None
    cached = counts["cachedInputTokens"]
    cache_write = counts["cacheWriteTokens"]
    uncached = max(0, counts["inputTokens"] - cached - cache_write)
    cost = (
        uncached * price["input"]
        + cached * price.get("cachedInput", price["input"])
        + cache_write * price.get("cacheWrite", price["input"])
        + counts["outputTokens"] * price["output"]
    ) / 1_000_000
    return round(cost, 6)


def empty_counts() -> dict[str, Any]:
    return {key: 0 for key in COUNTERS}


def record_usage(run: dict[str, Any], model: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    """Fold one response's ``usage`` block into ``run["usage"]`` (wire shape) and return it."""
    usage = usage or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    delta = {
        "calls": 1,
        "inputTokens": int(usage.get("input_tokens") or 0),
        "cachedInputTokens": int(input_details.get("cached_tokens") or 0),
        "cacheWriteTokens": int(input_details.get("cache_write_tokens") or 0),
        "outputTokens": int(usage.get("output_tokens") or 0),
        "reasoningTokens": int(output_details.get("reasoning_tokens") or 0),
    }
    delta["totalTokens"] = int(usage.get("total_tokens") or (delta["inputTokens"] + delta["outputTokens"]))
    total = run.get("usage") or {**empty_counts(), "byModel": []}
    entry = next((item for item in total["byModel"] if item["model"] == model), None)
    if entry is None:
        entry = {"model": model, **empty_counts()}
        total["byModel"].append(entry)
    for key in COUNTERS:
        entry[key] += delta[key]
        total[key] += delta[key]
    entry.pop("costUsd", None)
    cost = estimate_cost(model, entry)
    if cost is not None:
        entry["costUsd"] = cost
    known = [item.get("costUsd") for item in total["byModel"]]
    total.pop("costUsd", None)
    if all(value is not None for value in known):
        total["costUsd"] = round(sum(known), 6)
    run["usage"] = total
    return total
