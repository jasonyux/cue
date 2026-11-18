"""The desktop agent's loop: Responses API → tool calls → tool results → response, until a final answer.

A copy of ``run_responses_code_loop`` in ``app/responses_loop.py`` (which the scenario page keeps), with the tools
taken from ``tools.py`` instead of being fixed to exec_py. ``run.py`` next to this file starts the worker and the
desktop session and then runs this loop. The event kinds and the model-call log are the same as the original, so the
console's run view and replay read both.
"""

from __future__ import annotations

import json
from typing import Any

from ....errors import RunnerCoreError
from ....processes import assert_active, await_active
from ....responses_loop import record_model_call
from ....usage import record_usage
from .tools import Tool, follow, running_cell, tools_for


def invalid_response(message: str) -> RunnerCoreError:
    return RunnerCoreError(message, code="unexpected_model_response", status_code=400)


def classify_response(response: dict[str, Any], tools: dict[str, Tool]) -> dict[str, Any]:
    """Validate *all* output items before allowing any tool to run."""
    if not isinstance(response, dict):
        raise invalid_response("Malformed Responses API response.")
    if response.get("status") != "completed" or response.get("error"):
        error = response.get("error")
        message = error.get("message", "") if isinstance(error, dict) else ""
        raise invalid_response(
            f"Responses API request did not complete (status: {response.get('status', 'missing')}). {message}".strip()
        )
    if not isinstance(response.get("id"), str) or not response["id"].strip():
        raise invalid_response("Responses API response ID is missing.")
    if not isinstance(response.get("output"), list):
        raise invalid_response("Responses API output is missing.")
    calls: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    commentary: list[str] = []
    final: list[str] = []
    refusal: str | None = None
    for item in response["output"]:
        if not isinstance(item, dict):
            raise invalid_response("Malformed response output item.")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "function_call":
            name = item.get("name")
            tool = tools.get(name) if isinstance(name, str) else None
            if tool is None:
                raise invalid_response(f"Unexpected function call: {name or '<unknown>'}. Supported: {', '.join(tools)}.")
            if "status" in item and item["status"] != "completed":
                raise invalid_response("The response contains an unfinished function call.")
            if not isinstance(item.get("call_id"), str) or not item["call_id"].strip():
                raise invalid_response("Function call ID is missing.")
            if item["call_id"] in call_ids:
                raise invalid_response(f"Duplicate function call ID: {item['call_id']}.")
            if not isinstance(item.get("arguments"), str):
                raise invalid_response("Function call arguments must be a JSON string.")
            try:
                args = json.loads(item["arguments"])
            except ValueError as error:
                raise invalid_response("Function call arguments are not valid JSON.") from error
            if not isinstance(args, dict):
                raise invalid_response(f"{name} arguments must be a JSON object.")
            problem = tool.validate(args)
            if problem:
                raise invalid_response(problem)
            call_ids.add(item["call_id"])
            calls.append({"call_id": item["call_id"], "name": name, "arguments": item["arguments"], "args": args})
            continue
        if item.get("type") != "message":
            raise RunnerCoreError(
                f"Unsupported response output: {item.get('type')}. Only function tool calls are supported.",
                code="unsupported_tool_response",
                status_code=400,
            )
        unfinished = "status" in item and item["status"] != "completed"
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list) or unfinished:
            raise invalid_response("The response contains an invalid or unfinished assistant message.")
        if item.get("phase") not in (None, "commentary", "final_answer"):
            raise invalid_response("Unknown assistant message phase.")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise invalid_response("Malformed assistant message content.")
            if part.get("type") == "refusal":
                text = part.get("refusal")
                refusal = text.strip() if isinstance(text, str) and text.strip() else "The model declined this task."
                continue
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise invalid_response("Unsupported assistant message content.")
            text = part["text"].strip()
            if text:
                (commentary if item.get("phase") == "commentary" else final).append(text)
    if refusal:
        raise RunnerCoreError(refusal, code="model_refusal", status_code=400)
    progress = "\n\n".join(commentary)
    if calls:
        return {"kind": "calls", "calls": calls, "commentary": progress}
    if final:
        return {"kind": "final", "text": "\n\n".join(final), "commentary": progress}
    if commentary:
        return {"kind": "commentary", "commentary": progress}
    raise invalid_response("Responses API returned no tool calls or nonempty final assistant message.")


async def emit(context: Any, kind: str, level: str, message: str, detail: str) -> None:
    """One run event, unless the run was stopped meanwhile."""
    event = {"type": kind, "level": level, "message": message, "detail": detail}
    await await_active(context.emit_event(event), context.signal)


async def run_agent_loop(context: Any, session: Any, instructions: str, max_response_turns: int, client: Any) -> dict[str, Any]:
    """Each iteration requests a response and sends all matching tool results back."""
    tools = tools_for(session, context)
    previous_response_id: str | None = None
    next_input: Any = context.detail["run"]["prompt"].strip()
    final_assistant_message: str | None = None
    for turn in range(1, max_response_turns + 1):
        assert_active(context.signal)
        request: dict[str, Any] = {
            "instructions": instructions,
            "input": next_input,
            "model": context.detail["run"]["model"],
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
            "truncation": "auto",
            "tools": [tool.definition for tool in tools.values()],
        }
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        response = await await_active(client.create(request, context.signal), context.signal)
        assert_active(context.signal)
        await record_model_call(context, turn, request, response)
        record_usage(context.detail["run"], response.get("model") or request["model"], response.get("usage"))
        classified = classify_response(response, tools)
        usage = response.get("usage") or {}
        reasoning = (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
        await emit(
            context,
            "run_progress",
            "ok",
            f"Responses API turn {turn} completed.",
            f"{response['id']} · {usage.get('input_tokens', 0)} in · {usage.get('output_tokens', 0)} out · {reasoning} reasoning",
        )
        previous_response_id = response["id"]
        if classified["commentary"]:
            await emit(context, "run_progress", "pending", "Model progress.", classified["commentary"])
        if classified["kind"] == "final":
            final_assistant_message = classified["text"]
            break
        if classified["kind"] == "commentary":
            next_input = []
            continue
        tool_outputs = []
        for call in classified["calls"]:
            assert_active(context.signal)
            output = await tools[call["name"]].handler(context, session, call)
            assert_active(context.signal)
            tool_outputs.append({"call_id": call["call_id"], "output": output, "type": "function_call_output"})
        next_input = tool_outputs
    if (cell := running_cell(session)) is not None:  # the model finished while a cell still ran: stop it, keep its output
        cell.interrupt()
        await follow(context, session, cell, 15.0)
    if final_assistant_message is None:
        raise RuntimeError(
            f"The agent loop exhausted the configured {max_response_turns}-turn budget without producing a final assistant message."
        )
    await emit(context, "run_progress", "ok", "Model returned a final response.", final_assistant_message)
    return {
        "final_assistant_message": final_assistant_message,
        "notes": [
            "Executed the task through the live actor's agent loop.",
            f"Model final response: {final_assistant_message}",
        ],
    }
