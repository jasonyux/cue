import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import models

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = json.loads((ROOT / "contracts/fixtures.json").read_text())
MODELS = {
    "startRunRequestSchema": models.StartRunRequest,
    "browserScreenshotArtifactSchema": models.BrowserScreenshotArtifact,
    "runEventSchema": models.RunEvent,
    "runRecordSchema": models.RunRecord,
    "backendCapabilitiesSchema": models.BackendCapabilities,
    "runnerErrorResponseSchema": models.RunnerErrorResponse,
    "replayBundleSchema": models.ReplayBundle,
}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["name"])
def test_shared_wire_contract(fixture):
    model = MODELS[fixture["schema"]]
    if not fixture["valid"]:
        with pytest.raises(ValidationError):
            models.validate_wire(model, fixture["value"])
        return
    value = models.validate_wire(model, fixture["value"])
    assert value == fixture["value"]


@pytest.mark.parametrize("key,value", [("maxResponseTurns", True), ("verificationEnabled", 1),
                                      ("model", None), ("browserMode", None)])
def test_request_does_not_coerce_types_or_null(key, value):
    with pytest.raises(ValidationError):
        models.StartRunRequest.model_validate({"scenarioId": "desktop-free-task", "prompt": "Draw", key: value})


def test_a_chat_turn_before_any_worklog_line_carries_an_empty_worklog_ts():
    chat = {"turn": 1, "agent": "cmux", "text": "hi", "context": "No activity summaries yet.", "worklogTs": ""}
    request = models.StartRunRequest.model_validate({"scenarioId": "desktop-free-task", "prompt": "hi", "liveContext": True, "chat": chat})
    assert request.chat is not None and request.chat.worklog_ts == ""


def test_run_record_round_trips_usage():
    from app.models import RunRecord, validate_wire

    record = {"id": "run", "scenarioId": "s", "labId": "desktop", "browserMode": "headful", "model": "m",
              "maxResponseTurns": 5, "prompt": "p", "status": "completed", "startedAt": "2026-09-17T00:00:00Z",
              "usage": {"calls": 2, "inputTokens": 10, "cachedInputTokens": 1, "cacheWriteTokens": 2, "outputTokens": 3,
                        "reasoningTokens": 1, "totalTokens": 13, "costUsd": 0.5,
                        "byModel": [{"model": "m", "calls": 2, "inputTokens": 10, "cachedInputTokens": 1, "cacheWriteTokens": 2,
                                     "outputTokens": 3, "reasoningTokens": 1, "totalTokens": 13, "costUsd": 0.5}]}}
    assert validate_wire(RunRecord, record)["usage"] == record["usage"]
