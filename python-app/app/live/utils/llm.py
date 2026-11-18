"""Chat Completions client for one live role (summarizer, belief updater, proposer), independent of the task agent's model.
Claude on Bedrock Mantle goes through the Messages API instead, and OpenAI's own endpoint through the Responses API (where
gpt-6 takes tools with reasoning on), both translated to and from Chat Completions here."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from openai import BadRequestError

from ...credentials import bedrock_region, resolve_api_key
from ...responses_loop import summarize_images
from ..config import ModelEndpoint

THINKING_BUDGET = 16384
PLAIN_BUDGET = 3000
FINISH_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter"}
INCOMPLETE_REASONS = {"max_output_tokens": "length"}  # why a Responses API reply stopped short, as Chat Completions says it
# OpenAI's reasoning effort with thinking off: "none" from gpt-5.1 on, "minimal" for gpt-5, none at all for a model without
# reasoning. A client starts at the first and moves on when the endpoint refuses one, naming it as one of EFFORT_PARAMS.
PLAIN_EFFORTS: tuple[str | None, ...] = ("none", "minimal", None)
EFFORT_PARAMS = ("reasoning_effort", "reasoning.effort")  # Chat Completions', the Responses API's


@dataclass
class ModelReply:
    """One completion: what the model said, what it cost, and the exact request and response for the log."""

    content: str
    reasoning: str
    finish_reason: str | None
    model: str
    usage: dict[str, Any]
    images: int
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    fallback: str | None = None  # set by a loop that retried without thinking
    call_id: str = ""            # set by the loop when it logs the call
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"id": str, "name": str, "arguments": dict}], in order

    def message(self) -> dict[str, Any]:
        """The reply as an assistant message, to continue the conversation with the tools' results after it."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                for call in self.tool_calls
            ]
        return message


def truncated(reply: ModelReply) -> bool:
    """The model hit its output limit before writing an answer (all of the budget went to reasoning)."""
    return not reply.content.strip() and not reply.tool_calls and reply.finish_reason == "length"


def add_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    """Two usage blocks (``normalize_usage``'s shape) summed, counter by counter."""
    return {key: add_usage(total.get(key) or {}, value) if isinstance(value, dict) else (total.get(key) or 0) + value
            for key, value in usage.items()}


def parse_json_object(text: str) -> dict[str, Any]:
    """Accept a bare object, a fenced block, or an object embedded in prose; drop inline <think> blocks."""
    candidate = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("The model response contains no JSON object.")
        candidate = candidate[start:end + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("The model response is not a JSON object.")  # noqa: TRY004 - one error class for callers
    return value


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Chat Completions usage → the Responses-style shape app.usage.record_usage understands."""
    usage = usage or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "input_tokens_details": {"cached_tokens": int(prompt_details.get("cached_tokens") or 0),
                                 "cache_write_tokens": int(prompt_details.get("cache_write_tokens") or 0)},
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "output_tokens_details": {"reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0)},
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def assemble(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """A streamed completion folded back into the shape of a plain one: the deltas' text, reasoning and tool-call
    fragments concatenated, the finish reason from the chunk that carries it, the usage from the final chunk (sent
    when ``stream_options.include_usage`` is on). What the log and the parsing read is then the same either way."""
    message: dict[str, Any] = {"role": "assistant", "content": "", "reasoning_content": ""}
    calls: dict[int, dict[str, Any]] = {}  # by the index the deltas carry; fragments of one call share it
    finish_reason = None
    model = ""
    usage = None
    for chunk in chunks:
        model = chunk.get("model") or model
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            message["content"] += str(delta.get("content") or "")
            message["reasoning_content"] += str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            for fragment in delta.get("tool_calls") or []:
                call = calls.setdefault(int(fragment.get("index") or 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                call["id"] = fragment.get("id") or call["id"]
                function = fragment.get("function") or {}
                call["function"]["name"] = function.get("name") or call["function"]["name"]
                call["function"]["arguments"] += str(function.get("arguments") or "")
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            **({"usage": usage} if usage else {})}


def mantle_region(endpoint: ModelEndpoint) -> str | None:
    """The AWS region when ``endpoint`` is Claude on Bedrock Mantle, which serves Claude through the Messages API only
    (its Chat Completions route refuses Claude models); None for every other endpoint."""
    if endpoint.provider != "anthropic" or "bedrock-mantle." not in (endpoint.base_url or ""):
        return None
    return bedrock_region(endpoint.base_url)


def image_block(url: str) -> dict[str, Any]:
    match = re.match(r"data:(image/[\w.+-]+);base64,(.*)", url, re.DOTALL)
    if match:
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def messages_request(request: dict[str, Any], thinking: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A Chat Completions request as Messages API parameters: system messages lifted to ``system``, image parts and tool
    calls as content blocks, each run of tool results as one user turn. An assistant turn this client produced gets its
    thinking blocks back (``thinking``, keyed by the turn's first tool call id), which a tool loop with thinking needs."""
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for message in request["messages"]:
        role, content = message["role"], message.get("content")
        if role == "system":
            system.append({"type": "text", "text": str(content or "")})
            continue
        if role == "tool":
            result = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(content or "")}
            if turns and turns[-1]["role"] == "user" and turns[-1]["content"] and turns[-1]["content"][-1]["type"] == "tool_result":
                turns[-1]["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
            continue
        calls = message.get("tool_calls") or []
        blocks: list[dict[str, Any]] = list(thinking.get(calls[0]["id"], [])) if calls else []
        for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if part.get("type") == "image_url":
                blocks.append(image_block(part["image_url"]["url"]))
            elif part.get("text"):  # the API refuses an empty text block
                blocks.append({"type": "text", "text": part["text"]})
        for call in calls:
            blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                           "input": json.loads(call["function"].get("arguments") or "{}")})
        turns.append({"role": role, "content": blocks})
    params: dict[str, Any] = {"model": request["model"], "max_tokens": request["max_completion_tokens"], "messages": turns}
    if system:
        params["system"] = system
    if request.get("tools"):
        params["tools"] = [{"name": tool["function"]["name"], "description": tool["function"].get("description", ""),
                            "input_schema": tool["function"].get("parameters") or {"type": "object"}} for tool in request["tools"]]
        params["tool_choice"] = {"type": {"required": "any"}.get(request["tool_choice"], request["tool_choice"])}
    return params


def chat_payload(message: Any, model: str) -> dict[str, Any]:
    """A Messages API reply in the Chat Completions shape the rest of this client, and the call log, read. Its usage
    counts cache reads and writes into the prompt tokens, as Chat Completions does."""
    calls = [{"id": block.id, "type": "function", "function": {"name": block.name, "arguments": json.dumps(block.input)}}
             for block in message.content if block.type == "tool_use"]
    reply = {"role": "assistant", "content": "".join(block.text for block in message.content if block.type == "text"),
             "reasoning_content": "".join(block.thinking for block in message.content if block.type == "thinking"),
             **({"tool_calls": calls} if calls else {})}
    usage = message.usage
    cached, written = usage.cache_read_input_tokens or 0, usage.cache_creation_input_tokens or 0
    prompt = usage.input_tokens + cached + written
    return {"model": model, "choices": [{"index": 0, "message": reply, "finish_reason": FINISH_REASONS.get(message.stop_reason, message.stop_reason)}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": usage.output_tokens, "total_tokens": prompt + usage.output_tokens,
                      "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written}}}


def responses_request(request: dict[str, Any], reasoning: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A Chat Completions request as Responses API parameters: image parts as input images, tool calls and their results
    as function call items. An assistant turn this client produced gets its reasoning items back (``reasoning``, keyed by
    the turn's first tool call id), so a tool loop goes on from the model's own reasoning. Nothing is stored at OpenAI
    (the screen's content stays out of its logs), so the reasoning comes back encrypted with each reply that calls tools."""
    items: list[dict[str, Any]] = []
    for message in request["messages"]:
        role, content = message["role"], message.get("content")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": str(content or "")})
            continue
        calls = message.get("tool_calls") or []
        if calls:
            items.extend(reasoning.get(calls[0]["id"], []))
        if isinstance(content, list):
            items.append({"role": role, "content": [input_part(part) for part in content]})
        elif content:
            items.append({"role": role, "content": str(content)})
        for call in calls:
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"], "name": function["name"], "arguments": function.get("arguments") or "{}"})
    params: dict[str, Any] = {"model": request["model"], "input": items, "max_output_tokens": request["max_completion_tokens"], "store": False}
    if "reasoning_effort" in request:
        params["reasoning"] = {"effort": request["reasoning_effort"]}
    if request.get("tools"):
        params["tools"] = [function_tool(tool["function"]) for tool in request["tools"]]
        params["tool_choice"] = request["tool_choice"]
        params["include"] = ["reasoning.encrypted_content"]
    return params


def input_part(part: dict[str, Any]) -> dict[str, Any]:
    """A Chat Completions content part (text or image) as a Responses API one."""
    if part.get("type") == "image_url":
        return {"type": "input_image", "image_url": part["image_url"]["url"], "detail": "auto"}
    return {"type": "input_text", "text": str(part.get("text") or "")}


def function_tool(function: dict[str, Any]) -> dict[str, Any]:
    """A Chat Completions function schema as a Responses API tool, not strict, as it is on Chat Completions."""
    parameters = function.get("parameters") or {"type": "object"}
    return {"type": "function", "name": function["name"], "description": function.get("description", ""), "parameters": parameters, "strict": False}


def responses_payload(response: dict[str, Any], model: str) -> dict[str, Any]:
    """A Responses API reply in the Chat Completions shape the rest of this client, and the call log, read. A reply cut
    short at its output limit finishes with "length", so a loop's retry without thinking still sees it."""
    content, reasoning = "", ""
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if item.get("type") == "message":
            content += "".join(part.get("text") or "" for part in item.get("content") or [] if part.get("type") == "output_text")
        elif item.get("type") == "reasoning":
            reasoning += "".join(part.get("text") or "" for part in item.get("summary") or [])
        elif item.get("type") == "function_call":
            function = {"name": item.get("name") or "", "arguments": item.get("arguments") or "{}"}
            calls.append({"id": item.get("call_id") or "", "type": "function", "function": function})
    if response.get("status") == "incomplete":
        stopped = str((response.get("incomplete_details") or {}).get("reason") or "max_output_tokens")
        finish_reason = INCOMPLETE_REASONS.get(stopped, stopped)
    else:
        finish_reason = "tool_calls" if calls else "stop"
    reply = {"role": "assistant", "content": content, "reasoning_content": reasoning, **({"tool_calls": calls} if calls else {})}
    usage = response.get("usage") or {}
    return {
        "model": model,
        "choices": [{"index": 0, "message": reply, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
            "prompt_tokens_details": {"cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0},
            "completion_tokens_details": {"reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0},
        },
    }


class LiveModelClient:
    def __init__(self, endpoint: ModelEndpoint, *, timeout: float = 300) -> None:
        self.endpoint = endpoint
        base_url = endpoint.base_url  # resolved by the settings: the role's own, or its provider's (live/config.py)
        api_key = resolve_api_key(endpoint.api_key, base_url=base_url)
        self.region = mantle_region(endpoint)
        self.responses = endpoint.provider == "openai"  # OpenAI's own endpoint: the Responses API
        self.plain_effort = PLAIN_EFFORTS[0]  # the reasoning effort sent with thinking off, until the endpoint refuses it
        # Mantle's thinking blocks or the Responses API's reasoning items, by the first tool call id of the turn that thought them
        self.thinking_blocks: dict[str, list[dict[str, Any]]] = {}
        if self.region:
            from anthropic import AsyncAnthropicBedrockMantle

            # No key: SigV4 from the AWS credential chain (AWS_PROFILE); the key sentinel: a minted bearer token
            workspace = os.environ.get("ANTHROPIC_BEDROCK_MANTLE_WORKSPACE_ID")
            self.client: Any = AsyncAnthropicBedrockMantle(aws_region=self.region, api_key=api_key or None, timeout=timeout, max_retries=0,
                                                           default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        else:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(api_key=api_key or "empty", base_url=base_url, timeout=timeout, max_retries=0)

    async def complete(self, messages: list[dict[str, Any]], *, thinking: bool, images: list[str] | None = None,
                       max_tokens: int | None = None, temperature: float = 0.7,
                       tools: list[dict[str, Any]] | None = None, tool_choice: str = "auto") -> ModelReply:
        """main API for model calls/completion across providers. ``tool_choice`` "none" keeps the tools defined (a
        conversation that holds tool calls must keep defining them, on some providers) but has the model answer.
        """
        budget = max_tokens or (THINKING_BUDGET if thinking else PLAIN_BUDGET)
        if images:
            messages = [dict(message) for message in messages]
            last = messages[-1]
            parts: list[dict[str, Any]] = [{"type": "text", "text": str(last.get("content") or "")}]
            parts.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
            last["content"] = parts
        request: dict[str, Any] = {"model": self.endpoint.model, "messages": messages, "max_completion_tokens": budget}
        if tools:
            request.update(tools=tools, tool_choice=tool_choice)
        extra_body: dict[str, Any] = {}
        style = self.endpoint.provider
        if style == "litellm":
            style = "anthropic" if "claude" in self.endpoint.model.lower() else "openai"
        if style == "openai":
            if not thinking and self.plain_effort:
                request["reasoning_effort"] = self.plain_effort
        elif style == "anthropic":
            if thinking:
                extra_body = {"thinking": {"type": "adaptive"}}
        elif style == "openrouter":
            extra_body = {"reasoning": {"enabled": True} if thinking else {"effort": "none"}}
        else:
            request["temperature"] = temperature
            if not thinking:
                extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
        payload = await self.send(request, extra_body)
        content = reasoning = ""
        finish_reason = None
        tool_calls: list[dict[str, Any]] = []
        if payload.get("choices"):
            choice = payload["choices"][0]
            message = choice.get("message", {})
            content = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
            finish_reason = choice.get("finish_reason")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                tool_calls.append({"id": str(call.get("id") or ""), "name": str(function.get("name") or ""),
                                   "arguments": arguments if isinstance(arguments, dict) else {}})
        logged = {**request, **({"extra_body": extra_body} if extra_body else {})}
        if os.environ.get("CUA_LOG_MODEL_IMAGES", "").strip() not in ("1", "true", "yes"):
            logged = summarize_images(logged)
        return ModelReply(content=content, reasoning=reasoning, finish_reason=finish_reason,
                          model=str(payload.get("model") or self.endpoint.model), usage=normalize_usage(payload.get("usage")),
                          images=len(images or []), request=logged, response=payload, tool_calls=tool_calls)

    async def send(self, request: dict[str, Any], extra_body: dict[str, Any]) -> dict[str, Any]:
        """``request`` sent until the endpoint takes its reasoning effort: each OpenAI model family takes its own value
        with thinking off, so a refused one moves this client on to the next of PLAIN_EFFORTS, which it then keeps."""
        while True:
            try:
                return await self.send_once(request, extra_body)
            except BadRequestError as error:
                if "reasoning_effort" not in request or error.param not in EFFORT_PARAMS:
                    raise
                self.plain_effort = PLAIN_EFFORTS[PLAIN_EFFORTS.index(request.pop("reasoning_effort")) + 1]
                if self.plain_effort:
                    request["reasoning_effort"] = self.plain_effort

    async def send_once(self, request: dict[str, Any], extra_body: dict[str, Any]) -> dict[str, Any]:
        """One call through the endpoint's API, its reply as a Chat Completions payload."""
        if self.region:
            return await self.messages_completion(request, extra_body.get("thinking"))
        if self.responses:
            return await self.responses_completion(request)
        if self.endpoint.stream:
            request.update(stream=True, stream_options={"include_usage": True})
            chunks = await self.client.chat.completions.create(**request, extra_body=extra_body or None)
            return assemble([chunk.model_dump(exclude_none=True) async for chunk in chunks])
        response = await self.client.chat.completions.create(**request, extra_body=extra_body or None)
        return response.model_dump(exclude_none=True)

    async def responses_completion(self, request: dict[str, Any]) -> dict[str, Any]:
        """``request`` through the Responses API (OpenAI), its reply in the Chat Completions shape. Streamed, the reply is
        the response the stream's last event carries: completed, or incomplete when cut short."""
        params = responses_request(request, self.thinking_blocks)
        if self.endpoint.stream:
            response = None
            async for event in await self.client.responses.create(**params, stream=True):
                if event.type in ("response.completed", "response.incomplete", "response.failed"):
                    response = event.response
            if response is None:
                raise RuntimeError("The Responses API stream ended without a response.")
        else:
            response = await self.client.responses.create(**params)
        payload = response.model_dump(exclude_none=True)
        if payload.get("status") == "failed":
            raise RuntimeError(f"The response failed: {payload.get('error')}")
        output = payload.get("output") or []
        thought = [item for item in output if item.get("type") == "reasoning"]
        calls = [item.get("call_id") for item in output if item.get("type") == "function_call"]
        if thought and calls:
            self.thinking_blocks[calls[0]] = thought
        return responses_payload(payload, params["model"])

    async def messages_completion(self, request: dict[str, Any], thinking: dict[str, Any] | None) -> dict[str, Any]:
        """``request`` through the Messages API (Bedrock Mantle), its reply in the Chat Completions shape."""
        params = messages_request(request, self.thinking_blocks)
        if thinking:
            params["thinking"] = {**thinking, "display": "summarized"}  # the reasoning in the log, not an empty block
        if self.endpoint.stream:
            async with self.client.messages.stream(**params) as stream:
                message = await stream.get_final_message()
        else:
            message = await self.client.messages.create(**params)
        thought = [block.model_dump(exclude_none=True) for block in message.content if block.type in ("thinking", "redacted_thinking")]
        calls = [block.id for block in message.content if block.type == "tool_use"]
        if thought and calls:
            self.thinking_blocks[calls[0]] = thought
        return chat_payload(message, params["model"])

    async def close(self) -> None:
        await self.client.close()
