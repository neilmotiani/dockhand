import json
import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.extract.client import (
    BEARER_ENV,
    BedrockExtractor,
    ExtractionError,
    Usage,
    make_bedrock_client,
)
from app.extract.prompts import PROMPTS, get_prompt
from app.extract.schemas import EXTRACTION_JSON_SCHEMA, Carrier, ExtractionResult

GOOD = {
    "reasoning": "Three boxes, one with a UPS label.",
    "carrier": "UPS",
    "package_count": 3,
    "label_text": ["UPS 1Z 999 AA1 01 23 4567 84"],
    "confidence": 0.9,
}


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


def converse_response(text: str, stop: str = "end_turn", inp: int = 1500, out: int = 80) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": stop,
        "usage": {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out},
        "metrics": {"latencyMs": 1234},
    }


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def converse(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return self.responses.pop(0)


IMG = [(b"\x89PNG fake", "png")]


@pytest.fixture(autouse=True)
def isolated_bearer_env(monkeypatch):
    # make_bedrock_client writes os.environ; setenv-then-delenv makes monkeypatch restore the
    # original state (including "absent") after each test.
    monkeypatch.setenv(BEARER_ENV, "placeholder")
    monkeypatch.delenv(BEARER_ENV)


# --- client factory / auth --------------------------------------------------------------------


def test_bearer_token_exported_for_botocore(monkeypatch) -> None:
    make_bedrock_client(settings(aws_bearer_token_bedrock="tok-123"))
    assert os.environ[BEARER_ENV] == "tok-123"


def test_existing_env_token_not_overwritten(monkeypatch) -> None:
    monkeypatch.setenv(BEARER_ENV, "from-env")
    make_bedrock_client(settings(aws_bearer_token_bedrock="from-dotenv"))
    assert os.environ[BEARER_ENV] == "from-env"


def test_no_token_leaves_env_alone(monkeypatch) -> None:
    make_bedrock_client(settings())
    assert BEARER_ENV not in os.environ


def test_token_not_in_settings_repr() -> None:
    s = settings(aws_bearer_token_bedrock="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.model_dump())


@pytest.mark.parametrize("service", ["bedrock-runtime", "bedrock"])
def test_requests_use_bearer_auth(monkeypatch, service: str) -> None:
    # Intercept the signed request before it leaves the process and check the auth header.
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    client = make_bedrock_client(settings(aws_bearer_token_bedrock="tok-abc"), service)
    seen: dict[str, str] = {}

    class Sent(Exception):
        pass

    def capture(request, **_: Any) -> None:
        seen["auth"] = request.headers["Authorization"].decode()
        raise Sent

    client.meta.events.register("before-send", capture)
    with pytest.raises(Sent):
        if service == "bedrock":
            client.list_inference_profiles()
        else:
            client.converse(modelId="m", messages=[{"role": "user", "content": [{"text": "hi"}]}])
    assert seen["auth"] == "Bearer tok-abc"


# --- schema and prompts -----------------------------------------------------------------------


def test_json_schema_matches_pydantic_model() -> None:
    assert set(EXTRACTION_JSON_SCHEMA["properties"]) == set(ExtractionResult.model_fields)
    assert set(EXTRACTION_JSON_SCHEMA["required"]) == set(ExtractionResult.model_fields)
    assert EXTRACTION_JSON_SCHEMA["properties"]["carrier"]["enum"] == [c.value for c in Carrier]
    assert EXTRACTION_JSON_SCHEMA["additionalProperties"] is False
    # reasoning is generated first on purpose
    assert next(iter(EXTRACTION_JSON_SCHEMA["properties"])) == "reasoning"


def test_spec_carriers() -> None:
    assert [c.value for c in Carrier] == [
        "UPS", "FedEx", "USPS", "Amazon", "DHL", "Sysco", "US Foods", "Other", "Unknown"
    ]  # fmt: skip


@pytest.mark.parametrize(
    "patch",
    [{"confidence": 1.5}, {"confidence": -0.1}, {"package_count": -1}, {"carrier": "Royal Mail"}],
)
def test_result_rejects_out_of_range(patch: dict) -> None:
    with pytest.raises(ValueError):
        ExtractionResult.model_validate({**GOOD, **patch})


def test_result_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        ExtractionResult.model_validate({**GOOD, "tracking": "x"})


def test_prompts() -> None:
    assert "watermark" in get_prompt("v1")
    assert set(PROMPTS) >= {"v1"}
    with pytest.raises(ValueError, match="unknown extraction prompt"):
        get_prompt("v999")


def test_usage_add() -> None:
    u = Usage(input_tokens=1, output_tokens=2) + Usage(input_tokens=10, output_tokens=20)
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (11, 22, 33)


# --- extractor --------------------------------------------------------------------------------


def test_extract_success_and_request_shape() -> None:
    fake = FakeClient([converse_response(json.dumps(GOOD))])
    s = settings(
        bedrock_model_id="model-x", extraction_max_tokens=512, bedrock_structured_output=True
    )
    out = BedrockExtractor(fake, s).extract(IMG + [(b"jpg", "jpeg")])

    assert out.result == ExtractionResult.model_validate(GOOD)
    assert (out.model_id, out.prompt_version, out.attempts) == ("model-x", "v1", 1)
    assert out.usage == Usage(input_tokens=1500, output_tokens=80)
    assert out.bedrock_latency_ms == 1234
    assert out.latency_ms >= 0

    (call,) = fake.calls
    assert call["modelId"] == "model-x"
    assert call["system"] == [{"text": PROMPTS["v1"]}]
    assert call["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}
    content = call["messages"][0]["content"]
    assert content[0] == {"image": {"format": "png", "source": {"bytes": b"\x89PNG fake"}}}
    assert content[1]["image"]["format"] == "jpeg"
    assert "text" in content[2]
    fmt = call["outputConfig"]["textFormat"]
    assert fmt["type"] == "json_schema"
    assert json.loads(fmt["structure"]["jsonSchema"]["schema"]) == EXTRACTION_JSON_SCHEMA


@pytest.mark.parametrize(
    "bad",
    [
        converse_response("not json"),
        converse_response(json.dumps({**GOOD, "confidence": 2})),
        converse_response(json.dumps(GOOD)[:40], stop="max_tokens"),
        {"output": {"message": {"content": []}}, "stopReason": "end_turn"},  # no text, no usage
    ],
)
def test_retries_once_with_error_feedback(bad: dict) -> None:
    fake = FakeClient([bad, converse_response(json.dumps(GOOD), inp=1600)])
    out = BedrockExtractor(fake, settings()).extract(IMG)
    assert out.attempts == 2
    assert out.result.package_count == 3
    retry_msgs = fake.calls[1]["messages"]
    assert [m["role"] for m in retry_msgs] == ["user", "assistant", "user"]
    assert "invalid" in retry_msgs[2]["content"][0]["text"]


def test_usage_summed_across_attempts() -> None:
    fake = FakeClient(
        [
            converse_response("{}", inp=100, out=5),
            converse_response(json.dumps(GOOD), inp=200, out=50),
        ]
    )
    out = BedrockExtractor(fake, settings()).extract(IMG)
    assert out.usage == Usage(input_tokens=300, output_tokens=55)
    assert out.bedrock_latency_ms == 2468


def test_gives_up_after_two_invalid() -> None:
    fake = FakeClient([converse_response("nope"), converse_response("still nope")])
    with pytest.raises(ExtractionError, match="after 2 attempts"):
        BedrockExtractor(fake, settings()).extract(IMG)
    assert len(fake.calls) == 2


def test_client_errors_propagate() -> None:
    class Denied:
        def converse(self, **_: Any) -> dict:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse"
            )

    with pytest.raises(ClientError):
        BedrockExtractor(Denied(), settings()).extract(IMG)


def test_requires_images() -> None:
    with pytest.raises(ValueError, match="at least one image"):
        BedrockExtractor(FakeClient([]), settings()).extract([])


def test_unstructured_mode_puts_schema_in_prompt_and_strips_fences() -> None:
    from app.extract.client import SCHEMA_INSTRUCTION

    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    fake = FakeClient([converse_response(fenced)])
    out = BedrockExtractor(fake, settings(bedrock_structured_output=False)).extract(IMG)

    assert out.result == ExtractionResult.model_validate(GOOD)
    assert out.structured_output is False
    (call,) = fake.calls
    assert "outputConfig" not in call
    assert call["system"] == [{"text": PROMPTS["v1"] + SCHEMA_INSTRUCTION}]


def test_structured_mode_flagged() -> None:
    fake = FakeClient([converse_response(json.dumps(GOOD))])
    out = BedrockExtractor(fake, settings(bedrock_structured_output=True)).extract(IMG)
    assert out.structured_output is True
    assert "outputConfig" in fake.calls[0]
