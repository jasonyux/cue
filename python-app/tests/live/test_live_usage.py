"""The cost panel's rows: tokens and cost per model and module, over stored sessions, the last days, and the running session."""

from live_helpers import make_manager

from app.live.storage.paths import session_paths
from app.live.storage.usage import usage_by_day, usage_rows
from app.live.utils.jsonl import append_jsonl, write_json


def stage_call(paths, file, ts, model, tokens_in, tokens_out):
    append_jsonl(getattr(paths, file), {"ts": ts, "model": model,
                                        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out, "total_tokens": tokens_in + tokens_out}})


def test_usage_rows_per_model_and_module(tmp_path):
    old = session_paths(tmp_path, "20260901-100000").create()
    new = session_paths(tmp_path, "20260928-100000").create()
    stage_call(old, "summaries", "2026-09-01T10:01:00Z", "qwen", 1000, 10)
    stage_call(new, "summaries", "2026-09-28T10:01:00Z", "qwen", 500, 5)
    stage_call(new, "summaries", "2026-09-28T10:02:00Z", "qwen", 500, 5)
    stage_call(new, "beliefs", "2026-09-28T10:05:00Z", "qwen", 300, 30)
    append_jsonl(new.beliefs, {"ts": "2026-09-28T10:08:00Z", "model": "qwen", "usage": {"input_tokens": 100, "output_tokens": 10},  # a tick with the lookalike check
                               "embedding": {"model": "openai/text-embedding-3-small", "usage": {"input_tokens": 500, "output_tokens": 0}}})
    append_jsonl(new.proposals, {"ts": "2026-09-28T10:05:00Z", "error": "boom"})  # a failed tick has no usage and is not counted
    stage_call(new, "proposals", "2026-09-28T10:10:00Z", "gpt-6-sol", 2000, 100)
    stage_call(new, "feedback", "2026-09-28T10:12:00Z", "gpt-6-sol", 100, 10)  # the context call made when a decision is recorded
    append_jsonl(new.proposals, {"ts": "2026-09-28T10:14:00Z", "model": "gpt-6-sol", "usage": {"input_tokens": 50, "output_tokens": 5},  # a round with the duplicate filter
                                 "embedding": {"model": "openai/text-embedding-3-small", "usage": {"input_tokens": 400, "output_tokens": 0}}})
    run = new.runs_dir / "run-1"
    run.mkdir(parents=True)
    write_json(run / "run.json", {"id": "run-1", "startedAt": "2026-09-28T10:06:00Z", "usage": {"byModel": [
        {"model": "gpt-6-sol", "calls": 3, "inputTokens": 10_000, "cachedInputTokens": 8_000, "cacheWriteTokens": 0,
         "outputTokens": 100, "reasoningTokens": 0, "totalTokens": 10_100}]}})
    rows = usage_rows([old, new])
    assert [(row["model"], row["module"], row["calls"], row["inputTokens"], row["outputTokens"]) for row in rows] == [
        ("gpt-6-sol", "proposer", 2, 2050, 105), ("gpt-6-sol", "proposer: feedback digest", 1, 100, 10), ("gpt-6-sol", "agent", 3, 10_000, 100),
        ("openai/text-embedding-3-small", "proposer: duplicate filter", 1, 400, 0), ("openai/text-embedding-3-small", "belief updater: lookalike check", 1, 500, 0),
        ("qwen", "summarizer", 3, 2000, 20), ("qwen", "belief updater", 2, 400, 40)]
    assert rows[0]["costUsd"] == 0.00515 and rows[1]["costUsd"] == 0.0003 and rows[2]["costUsd"] == 0.0066  # sol: $2 in, $0.2 cached, $10 out
    assert rows[3]["costUsd"] == 0.000008 and rows[4]["costUsd"] == 0.00001  # embeddings: $0.02 per 1M in, nothing out
    assert "costUsd" not in rows[5]  # no price for qwen
    recent = usage_rows([old, new], since="2026-09-27T00:00:00Z")
    assert next(row for row in recent if row["module"] == "summarizer")["inputTokens"] == 1000
    assert usage_rows([old, new], since="2026-09-01T00:00:00Z", until="2026-09-02T00:00:00Z") == usage_rows([old])  # [since, until)
    by_day = usage_by_day([old, new])
    assert [day["day"] for day in by_day] == ["2026-09-01", "2026-09-28"] and by_day[1]["rows"] == recent
    assert usage_by_day([old, new], since="2026-09-27T00:00:00Z") == by_day[1:]


async def test_usage_report_covers_the_running_session(tmp_path):
    manager, _client, _ = make_manager(tmp_path / "data", ['{"summary": "s"}', "Nothing new.", '{"summary": "x", "proposals": []}'])
    assert manager.usage_report(3) == {"all": [], "recent": [], "byDay": [], "days": 3, "session": None}
    session_id = (await manager.start({"proposerIntervalS": 3600, "summaryIntervalS": 3600, "notify": False}))["session"]["id"]
    append_jsonl(session_paths(tmp_path / "data", session_id).snapshots,
                 {"kind": "screen_changed", "ts": "2026-09-17T10:00:00Z", "app": "Numbers", "window": "Budget", "tree": "[0] AXWindow"})
    await manager.propose_now()
    report = manager.usage_report(3)
    assert [(row["model"], row["module"], row["calls"], row["inputTokens"]) for row in report["session"]] == [
        ("fake-model", "summarizer", 1, 100), ("fake-model", "belief updater", 1, 100), ("fake-model", "proposer", 1, 100)]
    assert report["all"] == report["recent"] == report["session"]
    assert [day["rows"] for day in report["byDay"]] == [report["session"]]  # one day: today
    await manager.stop()
    assert manager.usage_report(3)["session"] is None and len(manager.usage_report(3)["all"]) == 3
