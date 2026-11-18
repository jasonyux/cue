"""JSON Lines and JSON file helpers used by every stage's records."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Records after byte ``offset`` plus the new offset, tolerating a partially written last line."""
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    with path.open("rb") as stream:
        stream.seek(offset)
        position = offset
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            position += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records, position


def tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    records, _ = read_jsonl(path)
    return records[-limit:] if limit > 0 else []


def rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Replace the whole file atomically; for the few records that are edited in place rather than appended."""
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text("".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records), encoding="utf-8")
    os.replace(temporary, path)


def amend_jsonl(path: Path, matches: Callable[[dict[str, Any]], bool], updates: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the file with ``updates`` applied to the last record ``matches`` accepts, and return that record."""
    records, _ = read_jsonl(path)
    index = next((i for i in range(len(records) - 1, -1, -1) if matches(records[i])), None)
    if index is None:
        raise ValueError(f"{path.name}: no record to amend")
    records[index] = {**records[index], **updates}
    rewrite_jsonl(path, records)
    return records[index]


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
