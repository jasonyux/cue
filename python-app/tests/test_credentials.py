import pytest

from app import credentials
from app.credentials import bedrock_bearer_token, bedrock_region, resolve_api_key
from app.errors import RunnerCoreError
from app.live.config import ModelEndpoint
from app.responses_loop import create_default_responses_client


@pytest.fixture(autouse=True)
def fresh_cache():
    credentials.clear_token_cache()
    yield
    credentials.clear_token_cache()


@pytest.mark.parametrize("url, expected", [
    ("https://bedrock-mantle.us-east-1.api.aws/openai/v1", "us-east-1"),
    ("https://bedrock-runtime.eu-west-2.amazonaws.com/openai/v1", "eu-west-2"),
    ("https://bedrock-mantle.us-gov-west-1.api.aws/v1", "us-gov-west-1"),
    ("https://api.openai.com/v1", None),
])
def test_region_is_read_from_bedrock_endpoints(monkeypatch, url, expected):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert bedrock_region(url) == expected
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    assert bedrock_region("https://api.openai.com/v1") == "ap-south-1"


def test_literal_keys_pass_through_and_the_sentinel_mints_a_cached_token():
    minted = []

    def generator(region):
        minted.append(region)
        return f"bedrock-api-key-{region}-{len(minted)}"

    clock = [1000.0]
    assert resolve_api_key("sk-literal") == "sk-literal" and resolve_api_key(None) is None
    first = resolve_api_key("aws-bedrock-token-generator", base_url="https://bedrock-mantle.us-east-1.api.aws/openai/v1",
                            generator=generator, now=lambda: clock[0])
    again = resolve_api_key(" AWS-Bedrock-Token-Generator ", base_url="https://bedrock-mantle.us-east-1.api.aws/openai/v1",
                            generator=generator, now=lambda: clock[0])
    assert first == "bedrock-api-key-us-east-1-1" and again == first and minted == ["us-east-1"]
    clock[0] += credentials.TOKEN_REFRESH_S + 1
    refreshed = bedrock_bearer_token(region="us-east-1", generator=generator, now=lambda: clock[0])
    assert refreshed == "bedrock-api-key-us-east-1-2"


def test_token_failures_become_runner_errors_with_hints(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(RunnerCoreError, match="which AWS region") as missing:
        bedrock_bearer_token(base_url="https://api.openai.com/v1", generator=lambda region: "x")
    assert missing.value.code == "aws_token_failed"

    def broken(region):
        raise RuntimeError("No valid AWS credentials")

    with pytest.raises(RunnerCoreError, match="No valid AWS credentials") as failed:
        bedrock_bearer_token(region="us-east-1", generator=broken)
    assert "aws configure" in (failed.value.hint or "")
    with pytest.raises(RunnerCoreError, match="empty token"):
        bedrock_bearer_token(region="us-east-1", generator=lambda region: "")


def test_task_agent_and_live_clients_use_the_generated_token(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "aws-bedrock-token-generator")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://bedrock-mantle.us-west-2.api.aws/openai/v1")
    monkeypatch.setattr(credentials, "_cache", {"us-west-2": ("bedrock-api-key-cached", 10**12)})
    monkeypatch.setattr(credentials.time, "time", lambda: 10**12 + 1)
    task_client = create_default_responses_client()
    assert task_client.client.api_key == "bedrock-api-key-cached"

    from app.live.utils.llm import LiveModelClient

    live_client = LiveModelClient(ModelEndpoint.resolved("openai"))  # a role on the openai provider reads its key set: OPENAI_*
    assert live_client.client.api_key == "bedrock-api-key-cached"
    explicit = LiveModelClient(ModelEndpoint(model="m", api_key="empty", base_url="http://vllm/v1"))
    assert explicit.client.api_key == "empty"
