"""The live model client: the Chat Completions request each provider gets, and usage normalized for app.usage."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from app.live.config import ModelEndpoint
from app.live.utils.llm import LiveModelClient, truncated

USAGE = {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17, "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2}}
# A streamed reply as the SSE chunks arrive: reasoning first, then text, then one tool call in fragments (its id and
# name once, its arguments in pieces), the finish reason, and a final chunk with only the usage.
CHUNKS = [
    {"model": "m-2", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "think"}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"reasoning_content": "ing"}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"content": "O"}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"content": "K"}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_7", "function": {"name": "add_task", "arguments": ""}}]}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"title": "Fi'}}]}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'nish"}'}}]}}]},
    {"model": "m-2", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {"model": "m-2", "choices": [], "usage": USAGE},
]


class FakeCompletions:
    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            async def chunks():
                for chunk in CHUNKS:
                    yield SimpleNamespace(model_dump=lambda exclude_none=True, chunk=chunk: chunk)
            return chunks()
        payload = {"model": kwargs["model"], "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": USAGE}
        return SimpleNamespace(model_dump=lambda exclude_none=True: payload)


def make_client(provider, stream=False, model="m"):
    client = LiveModelClient(ModelEndpoint(model=model, base_url="http://host/v1", api_key="k", provider=provider, stream=stream))
    completions = FakeCompletions()
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


HI = [{"role": "user", "content": "hi"}]
RESPONSES_USAGE = {
    "input_tokens": 13,
    "input_tokens_details": {"cached_tokens": 3},
    "output_tokens": 4,
    "output_tokens_details": {"reasoning_tokens": 2},
    "total_tokens": 17,
}
RESPONSE = {
    "model": "m-2026-01-01",
    "status": "completed",
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
    "usage": RESPONSES_USAGE,
}


def refusal(param):
    """The 400 OpenAI answers a parameter the model does not take with, naming the parameter."""
    response = httpx.Response(400, request=httpx.Request("POST", "http://host/v1/responses"))
    return BadRequestError(f"Unsupported value for '{param}'.", response=response, body={"param": param, "code": "unsupported_value"})


class FakeResponses:
    """The Responses API: each call takes the next scripted response (RESPONSE when none is left), as events when streamed;
    a call whose reasoning effort is in ``refuse`` (None: no effort) gets the 400 that names ``param``."""

    def __init__(self, replies=(), refuse=(), param="reasoning.effort"):
        self.requests, self.replies, self.refuse, self.param = [], list(replies), set(refuse), param

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if (kwargs.get("reasoning") or {}).get("effort") in self.refuse:
            raise refusal(self.param)
        response = self.replies.pop(0) if self.replies else RESPONSE
        dumped = SimpleNamespace(model_dump=lambda exclude_none=True: response)
        if not kwargs.get("stream"):
            return dumped

        async def events():
            started = SimpleNamespace(model_dump=lambda exclude_none=True: {"status": "in_progress"})
            yield SimpleNamespace(type="response.created", response=started)
            yield SimpleNamespace(type=f"response.{response['status']}", response=dumped)
        return events()


def make_responses_client(stream=False, **fake):
    client = LiveModelClient(ModelEndpoint(model="m", base_url="http://host/v1", api_key="k", provider="openai", stream=stream))
    responses = FakeResponses(**fake)
    client.client = SimpleNamespace(responses=responses)
    return client, responses


async def test_vllm_requests_carry_a_temperature_and_the_chat_template_thinking_flag():
    client, completions = make_client("vllm")
    reply = await client.complete([{"role": "user", "content": "hi"}], thinking=False, max_tokens=16)
    off = completions.requests[0]
    assert off["max_completion_tokens"] == 16 and off["temperature"] == 0.7 and "max_tokens" not in off and "reasoning_effort" not in off
    assert off["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    await client.complete([{"role": "user", "content": "hi"}], thinking=True)
    on = completions.requests[1]
    assert on["extra_body"] is None and on["max_completion_tokens"] == 16384 and on["temperature"] == 0.7
    assert reply.content == "OK" and reply.usage["input_tokens"] == 13 and reply.request["extra_body"] == off["extra_body"]
    assert reply.usage["input_tokens_details"] == {"cached_tokens": 3, "cache_write_tokens": 2}


async def test_openai_requests_go_through_the_responses_api_with_an_effort_and_no_temperature():
    client, responses = make_responses_client()
    reply = await client.complete([{"role": "system", "content": "Be brief."}, *HI], thinking=False, max_tokens=16)
    await client.complete(HI, thinking=True)
    off, on = responses.requests
    assert off["reasoning"] == {"effort": "none"} and off["max_output_tokens"] == 16 and off["store"] is False and "temperature" not in off
    assert off["input"] == [{"role": "system", "content": "Be brief."}, *HI] and "tools" not in off and "include" not in off
    assert "reasoning" not in on and on["max_output_tokens"] == 16384  # thinking on: the model's own effort
    assert reply.content == "OK" and reply.finish_reason == "stop" and reply.request["reasoning_effort"] == "none"
    assert reply.model == "m"  # the model asked for, as priced, not the dated snapshot that answered
    assert reply.usage["input_tokens"] == 13 and reply.usage["input_tokens_details"]["cached_tokens"] == 3
    assert reply.usage["output_tokens"] == 4 and reply.usage["output_tokens_details"] == {"reasoning_tokens": 2}


async def test_a_refused_thinking_off_effort_moves_on_to_the_next_one_and_keeps_it():
    """gpt-6 takes "none" only, gpt-5 "minimal" only, a model without reasoning no effort at all; each refusal names it."""
    client, responses = make_responses_client(refuse={"none"})
    await client.complete(HI, thinking=False)
    reply = await client.complete(HI, thinking=False)
    assert [request["reasoning"] for request in responses.requests] == [{"effort": "none"}, {"effort": "minimal"}, {"effort": "minimal"}]
    assert reply.request["reasoning_effort"] == "minimal"
    plain, responses = make_responses_client(refuse={"none", "minimal"})
    reply = await plain.complete(HI, thinking=False)
    assert [request.get("reasoning") for request in responses.requests] == [{"effort": "none"}, {"effort": "minimal"}, None]
    assert reply.content == "OK" and "reasoning_effort" not in reply.request
    other, responses = make_responses_client(refuse={"none"}, param="max_output_tokens")
    with pytest.raises(BadRequestError):  # a refusal of anything else is the caller's
        await other.complete(HI, thinking=False)
    thinking, responses = make_responses_client(refuse={None})
    with pytest.raises(BadRequestError):  # thinking on sends no effort, so there is none to move on from
        await thinking.complete(HI, thinking=True)
    assert len(responses.requests) == 1


async def test_a_tool_loop_through_the_responses_api_keeps_its_image_calls_results_and_reasoning():
    calling = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Look it up."}], "encrypted_content": "enc"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search_history", "arguments": '{"query": "deck"}'},
        ],
        "usage": RESPONSES_USAGE,
    }
    client, responses = make_responses_client(replies=[calling])
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    tools = [{"type": "function", "function": {"name": "search_history", "description": "Search.", "parameters": schema}}]
    first = await client.complete([{"role": "user", "content": "Next?"}], thinking=True, images=["data:image/png;base64,AAAA"], tools=tools)
    assert first.tool_calls == [{"id": "call_1", "name": "search_history", "arguments": {"query": "deck"}}]
    assert first.finish_reason == "tool_calls" and first.reasoning == "Look it up." and first.content == ""
    sent = responses.requests[0]
    image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"}
    assert sent["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Next?"}, image]}]
    assert sent["tools"] == [{"type": "function", "name": "search_history", "description": "Search.", "parameters": schema, "strict": False}]
    assert sent["tool_choice"] == "auto" and sent["include"] == ["reasoning.encrypted_content"]
    loop = [{"role": "user", "content": "Next?"}, first.message(), {"role": "tool", "tool_call_id": "call_1", "content": "2 hits"}]
    second = await client.complete(loop, thinking=True, tools=tools, tool_choice="none")
    assert responses.requests[1]["input"][1:] == [
        calling["output"][0],  # its own reasoning back, before the call it led to
        {"type": "function_call", "call_id": "call_1", "name": "search_history", "arguments": '{"query": "deck"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "2 hits"},
    ]
    assert responses.requests[1]["tool_choice"] == "none" and second.content == "OK" and not second.tool_calls


async def test_a_streamed_response_is_the_one_its_last_event_carries():
    cut = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "id": "rs_2", "summary": []}],
        "usage": RESPONSES_USAGE,
    }
    client, responses = make_responses_client(stream=True, replies=[cut, RESPONSE])
    reply = await client.complete(HI, thinking=True)
    assert responses.requests[0]["stream"] is True and reply.finish_reason == "length" and truncated(reply)  # a loop retries it
    assert (await client.complete(HI, thinking=False)).content == "OK"
    failed, _ = make_responses_client(stream=True, replies=[{"status": "failed", "error": {"code": "server_error", "message": "boom"}}])
    with pytest.raises(RuntimeError, match="boom"):
        await failed.complete(HI, thinking=True)


async def test_a_litellm_proxy_gets_each_model_s_own_parameters():
    """A Claude behind the proxy takes Anthropic's block (a reasoning_effort becomes a thinking budget Claude 5.5 refuses);
    any other model takes OpenAI's reasoning_effort, on Chat Completions."""
    client, completions = make_client("litellm", model="anthropic/claude-sonnet-5-5")
    await client.complete([{"role": "user", "content": "hi"}], thinking=False, max_tokens=16)
    await client.complete([{"role": "user", "content": "hi"}], thinking=True)
    off, on = completions.requests
    assert "reasoning_effort" not in off and off["extra_body"] is None and "temperature" not in off
    assert on["extra_body"] == {"thinking": {"type": "adaptive"}} and "reasoning_effort" not in on
    client, completions = make_client("litellm", model="openai/gpt-5")
    await client.complete([{"role": "user", "content": "hi"}], thinking=False, max_tokens=16)
    await client.complete([{"role": "user", "content": "hi"}], thinking=True)
    off, on = completions.requests
    assert off["reasoning_effort"] == "none" and off["extra_body"] is None and "reasoning_effort" not in on and on["extra_body"] is None


async def test_anthropic_endpoints_get_adaptive_thinking_and_never_a_reasoning_effort():
    client, completions = make_client("anthropic")
    await client.complete([{"role": "user", "content": "hi"}], thinking=False, max_tokens=16)
    await client.complete([{"role": "user", "content": "hi"}], thinking=True)
    off, on = completions.requests
    assert off["extra_body"] is None and "reasoning_effort" not in off and "temperature" not in off
    assert on["extra_body"] == {"thinking": {"type": "adaptive"}} and "reasoning_effort" not in on and "temperature" not in on


async def test_openrouter_endpoints_get_its_own_reasoning_switch_for_any_model():
    client, completions = make_client("openrouter")
    await client.complete([{"role": "user", "content": "hi"}], thinking=False, max_tokens=16)
    await client.complete([{"role": "user", "content": "hi"}], thinking=True)
    off, on = completions.requests
    assert off["extra_body"] == {"reasoning": {"effort": "none"}} and "reasoning_effort" not in off and "temperature" not in off
    assert on["extra_body"] == {"reasoning": {"enabled": True}} and "reasoning_effort" not in on and "temperature" not in on


async def test_a_streaming_endpoint_gets_the_same_reply_assembled_from_its_chunks():
    client, completions = make_client("vllm", stream=True)
    reply = await client.complete([{"role": "user", "content": "hi"}], thinking=True, tools=[{"type": "function", "function": {"name": "add_task"}}])
    request = completions.requests[0]
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True} and reply.request["stream"] is True
    assert reply.content == "OK" and reply.reasoning == "thinking" and reply.finish_reason == "tool_calls" and reply.model == "m-2"
    assert reply.tool_calls == [{"id": "call_7", "name": "add_task", "arguments": {"title": "Finish"}}]
    assert reply.usage["input_tokens"] == 13 and reply.usage["output_tokens"] == 4  # from the final, usage-only chunk
    assert reply.response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"title": "Finish"}'  # logged like a plain reply
    assert reply.message()["tool_calls"][0]["id"] == "call_7"
    plain, _ = make_client("vllm")
    assert "stream" not in (await plain.complete([{"role": "user", "content": "hi"}], thinking=True)).request


async def test_the_embedding_client_embeds_each_text_once_and_keeps_the_vectors_on_disk(tmp_path):
    import numpy as np

    from app.live.utils.embed import EmbeddingClient, similarities, unit

    class FakeEmbeddings:
        def __init__(self):
            self.requests = []

        async def create(self, **kwargs):
            self.requests.append(kwargs)
            payload = {"model": "embed-1", "data": [{"index": i, "embedding": [float(len(text)), 1.0]} for i, text in enumerate(kwargs["input"])],
                       "usage": {"prompt_tokens": 7, "total_tokens": 7}}
            return SimpleNamespace(model_dump=lambda exclude_none=True: payload)

    cache = tmp_path / "embeddings.jsonl"
    client = EmbeddingClient(ModelEndpoint(model="embed-1", base_url="http://host/v1", api_key="k"), cache)
    fake = FakeEmbeddings()
    client.client = SimpleNamespace(embeddings=SimpleNamespace(create=fake.create))
    vectors = await client.embed(["abc", "de", "abc"])
    assert [v.tolist() for v in vectors] == [unit([3.0, 1.0]).tolist()] + [unit([2.0, 1.0]).tolist()] + [unit([3.0, 1.0]).tolist()]
    assert vectors[0].dtype == np.float32 and fake.requests == [{"model": "embed-1", "input": ["abc", "de"]}]  # unit float32; distinct, sorted, once
    assert [v.tolist() for v in await client.embed(["de", "fghi"])] == [unit([2.0, 1.0]).tolist(), unit([4.0, 1.0]).tolist()]
    assert fake.requests[1]["input"] == ["fghi"]  # only the miss
    assert client.calls[1] == {"request": {"model": "embed-1", "input": ["fghi"]}, "response": {"model": "embed-1", "usage": {"prompt_tokens": 7, "total_tokens": 7}}}
    rows = [json.loads(line) for line in cache.read_text().splitlines()]
    assert [(row["model"], row["text"]) for row in rows] == [("embed-1", "abc"), ("embed-1", "de"), ("embed-1", "fghi")]
    again = EmbeddingClient(ModelEndpoint(model="embed-1", base_url="http://host/v1", api_key="k"), cache)  # a new session: the cache is loaded
    again.client = SimpleNamespace(embeddings=SimpleNamespace(create=fake.create))
    assert [v.tolist() for v in await again.embed(["fghi", "abc"])] == [unit([4.0, 1.0]).tolist(), unit([3.0, 1.0]).tolist()] and len(fake.requests) == 2
    assert json.loads(cache.read_text().splitlines()[0])["vector"] == [3.0, 1.0]  # stored as the endpoint sent it, not normalized
    other = EmbeddingClient(ModelEndpoint(model="embed-2", base_url="http://host/v1", api_key="k"), cache)  # another model: its own vectors
    assert other.vectors == {}
    scores = similarities([unit([1.0, 0.0]), unit([1.0, 0.5])], [unit([1.0, 0.0]), unit([0.0, 1.0])])
    assert scores.shape == (2, 2) and [round(float(s), 3) for s in scores.flatten()] == [1.0, 0.0, 0.894, 0.447]
