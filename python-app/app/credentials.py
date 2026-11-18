"""API key resolution.

``OPENAI_API_KEY`` is normally a literal key. Set it to the sentinel ``aws-bedrock-token-generator`` and the
runner instead mints short-lived Bedrock bearer tokens from the local AWS credential chain (``aws configure``,
environment variables, or an assumed role) using the ``aws-bedrock-token-generator`` package. Tokens are cached
and re-minted well before their 12-hour expiry, so long-running sessions keep working without key rotation.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from typing import Any

from .errors import RunnerCoreError

TOKEN_GENERATOR = "aws-bedrock-token-generator"
TOKEN_REFRESH_S = 6 * 3600  # tokens live 12 hours; mint a fresh one after six
_cache: dict[str, tuple[str, float]] = {}


def bedrock_region(base_url: str | None) -> str | None:
    """Region from a Bedrock endpoint such as https://bedrock-mantle.us-east-1.api.aws/openai/v1, else the AWS env."""
    if base_url:
        match = re.search(r"bedrock-(?:mantle|runtime)\.([a-z]{2}(?:-gov)?-[a-z]+-\d)\.", base_url)
        if match:
            return match.group(1)
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None


def bedrock_bearer_token(*, base_url: str | None = None, region: str | None = None,
                         generator: Callable[..., str] | None = None, now: Callable[[], float] = time.time) -> str:
    """A cached Bedrock bearer token for the region implied by ``base_url``."""
    region = region or bedrock_region(base_url)
    if not region:
        raise RunnerCoreError(
            "Cannot tell which AWS region to mint a Bedrock token for.", code="aws_token_failed", status_code=400,
            hint="Point OPENAI_BASE_URL at a bedrock-mantle or bedrock-runtime endpoint, or set AWS_REGION.",
        )
    cached = _cache.get(region)
    if cached and now() - cached[1] < TOKEN_REFRESH_S:
        return cached[0]
    mint = generator
    if mint is None:
        try:
            from aws_bedrock_token_generator import provide_token
        except ImportError as error:
            raise RunnerCoreError(
                "aws-bedrock-token-generator is not installed.", code="aws_token_failed", status_code=500,
                hint="Run `uv sync --project python-app` (the package is in pyproject.toml) and restart the runner.",
            ) from error
        mint = provide_token
    try:
        token = str(mint(region=region))
    except Exception as error:
        raise RunnerCoreError(
            f"Could not mint a Bedrock bearer token for {region}: {error}", code="aws_token_failed", status_code=500,
            hint="Check `aws configure` / AWS_PROFILE credentials and that they may call Bedrock in that region.",
        ) from error
    if not token:
        raise RunnerCoreError("The Bedrock token generator returned an empty token.", code="aws_token_failed", status_code=500)
    _cache[region] = (token, now())
    return token


def resolve_api_key(value: str | None, *, base_url: str | None = None, **options: Any) -> str | None:
    """A literal key passes through; the sentinel becomes a freshly minted (or cached) Bedrock token."""
    if value is None:
        return None
    if value.strip().lower() == TOKEN_GENERATOR:
        return bedrock_bearer_token(base_url=base_url, **options)
    return value


def clear_token_cache() -> None:
    _cache.clear()
