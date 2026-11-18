"""Text embeddings API
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ...credentials import resolve_api_key
from ..config import ModelEndpoint
from .jsonl import append_jsonl

Vector = np.ndarray  # float32, unit length


class EmbeddingClient:
    def __init__(self, endpoint: ModelEndpoint, cache: Path, *, timeout: float = 60) -> None:
        from openai import AsyncOpenAI

        self.endpoint = endpoint
        self.cache = cache
        base_url = endpoint.base_url  # resolved by the settings: the role's own, or its provider's (live/config.py)
        api_key = resolve_api_key(endpoint.api_key, base_url=base_url) or "empty"
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        self.vectors: dict[str, Vector] = {}
        if cache.exists():
            with cache.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        if row["model"] == endpoint.model:
                            self.vectors[row["text"]] = unit(row["vector"])
        self.calls: list[dict[str, Any]] = []  # each request made and what came back (minus the vectors), for the log

    async def embed(self, texts: list[str]) -> list[Vector]:
        """One unit vector per text, in order; texts not in the cache are fetched in one request and cached."""
        missing = sorted({text for text in texts if text not in self.vectors})
        if missing:
            response = await self.client.embeddings.create(model=self.endpoint.model, input=missing)
            payload = response.model_dump(exclude_none=True)
            for text, item in zip(missing, payload["data"], strict=True):
                self.vectors[text] = unit(item["embedding"])
                append_jsonl(self.cache, {"model": self.endpoint.model, "text": text, "vector": item["embedding"]})
            self.calls.append({"request": {"model": self.endpoint.model, "input": missing},
                               "response": {key: value for key, value in payload.items() if key != "data"}})
        return [self.vectors[text] for text in texts]

    async def close(self) -> None:
        await self.client.close()


def unit(vector: list[float]) -> Vector:
    """The vector as float32 at unit length, so a cosine is a plain dot product."""
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm else array


def similarities(queries: list[Vector], references: list[Vector]) -> np.ndarray:
    """Cosine of every query against every reference: a (queries × references) matrix, one product."""
    return np.stack(queries) @ np.stack(references).T
