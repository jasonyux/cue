"""Shared helpers: JSON Lines reading, model output parsing, usage, image encoding."""

import base64
import io

import pytest
from PIL import Image

from app.live.utils.images import encode_screenshot
from app.live.utils.jsonl import append_jsonl, read_jsonl, tail_jsonl
from app.live.utils.llm import normalize_usage, parse_json_object


def test_read_jsonl_returns_offsets_and_skips_partial_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})
    records, offset = read_jsonl(path)
    assert [item["n"] for item in records] == [1, 2]
    with path.open("a") as stream:
        stream.write('{"n": 3')  # partial write in progress
    more, again = read_jsonl(path, offset)
    assert more == [] and again == offset
    with path.open("a") as stream:
        stream.write("}\n")
    more, _ = read_jsonl(path, offset)
    assert [item["n"] for item in more] == [3]
    assert tail_jsonl(path, 2)[-1]["n"] == 3 and tail_jsonl(tmp_path / "missing.jsonl", 3) == []


@pytest.mark.parametrize("text", [
    '{"summary": "x", "action": "none"}',
    'Sure:\n```json\n{"summary": "x", "action": "none"}\n```',
    '<think>hmm {not json} </think>\n{"summary": "x", "action": "none"}',
    'Here you go {"summary": "x", "action": "none"} thanks',
])
def test_parse_json_object_variants(text):
    assert parse_json_object(text)["summary"] == "x"


def test_parse_json_object_rejects_non_objects():
    for text in ("no json here", "[1, 2]"):
        with pytest.raises(ValueError):
            parse_json_object(text)


def test_normalize_usage_maps_chat_completion_fields():
    usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                             "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 3}})
    assert usage["input_tokens"] == 10 and usage["input_tokens_details"]["cached_tokens"] == 4
    assert usage["output_tokens_details"]["reasoning_tokens"] == 3 and normalize_usage(None)["total_tokens"] == 0


def test_encode_screenshot_downscales_to_the_long_side(tmp_path):
    Image.new("RGB", (1600, 400)).save(tmp_path / "wide.png")
    url = encode_screenshot(tmp_path / "wide.png", max_side=400)
    assert url and url.startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
        assert decoded.size == (400, 100)
    assert encode_screenshot(tmp_path / "missing.png") is None
