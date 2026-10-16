"""Bedrock client factory and the Converse-based extractor."""

import os
import re
import time
from typing import Any, Literal

import boto3
from botocore.config import Config
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.extract.prompts import get_prompt
from app.extract.schemas import EXTRACTION_JSON_SCHEMA_STR, ExtractionResult

ImageFormat = Literal["png", "jpeg", "gif", "webp"]

# botocore reads Bedrock API keys only from this environment variable (it has no client
# parameter for it), and only for services whose signing name is "bedrock".
BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"


def make_bedrock_client(settings: Settings, service: str = "bedrock-runtime") -> Any:
    """Create a boto3 client for `bedrock-runtime` (or `bedrock` for control-plane calls).

    If a Bedrock API key is configured (e.g. in .env, which boto3 does not read), export it so
    botocore uses bearer auth. Otherwise boto3 uses the normal credential chain with SigV4.
    """
    token = settings.aws_bearer_token_bedrock
    if token is not None and not os.environ.get(BEARER_ENV):
        os.environ[BEARER_ENV] = token.get_secret_value()
    return boto3.client(
        service,
        region_name=settings.aws_region,
        config=Config(
            read_timeout=settings.bedrock_timeout_s,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ExtractionOutcome(BaseModel):
    result: ExtractionResult
    model_id: str
    prompt_version: str
    attempts: int
    usage: Usage  # summed over attempts
    latency_ms: float  # wall clock for the whole step, including a retry
    bedrock_latency_ms: int  # server-side latency reported by Converse, summed over attempts
    # False when the model could not take Converse `outputConfig` and the schema went in the
    # system prompt instead. Results from the two modes are not directly comparable.
    structured_output: bool = True


class ExtractionError(RuntimeError):
    pass


def _response_text(resp: dict) -> str:
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(b["text"] for b in blocks if "text" in b)


# Used only when structured output is off (models such as Amazon Nova reject `outputConfig`).
# Appended to the versioned prompt rather than baked into it, so v1 stays byte-identical for
# models that do support structured output.
SCHEMA_INSTRUCTION = (
    "\nRespond with a single JSON object and nothing else (no Markdown, no prose). "
    "It must validate against this JSON Schema:\n" + EXTRACTION_JSON_SCHEMA_STR
)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


class BedrockExtractor:
    """Send frames of one delivery event to a vision model and parse the structured result.

    Uses Converse structured output (a JSON schema in `outputConfig`) so the model is
    constrained to the schema, then validates with Pydantic as a second line of defence. If
    the output still fails validation, it retries once, showing the model the error.
    """

    MAX_ATTEMPTS = 2

    def __init__(self, client: Any, settings: Settings) -> None:
        self._client = client
        self.model_id = settings.bedrock_model_id
        self.prompt_version = settings.extraction_prompt_version
        self._system = get_prompt(self.prompt_version)
        self._max_tokens = settings.extraction_max_tokens
        self.structured_output = settings.bedrock_structured_output
        if not self.structured_output:
            self._system += SCHEMA_INSTRUCTION

    def extract(self, images: list[tuple[bytes, ImageFormat]]) -> ExtractionOutcome:
        if not images:
            raise ValueError("at least one image is required")
        content: list[dict] = [
            {"image": {"format": fmt, "source": {"bytes": b}}} for b, fmt in images
        ]
        content.append({"text": "Extract the delivery details from these frames."})
        messages: list[dict] = [{"role": "user", "content": content}]

        usage = Usage()
        server_ms = 0
        start = time.perf_counter()
        last_error = ""
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            request: dict[str, Any] = {
                "modelId": self.model_id,
                "system": [{"text": self._system}],
                "messages": messages,
                "inferenceConfig": {"maxTokens": self._max_tokens, "temperature": 0},
            }
            if self.structured_output:
                request["outputConfig"] = {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "name": "delivery_extraction",
                                "schema": EXTRACTION_JSON_SCHEMA_STR,
                            }
                        },
                    }
                }
            resp = self._client.converse(**request)
            u = resp.get("usage", {})
            usage += Usage(
                input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0)
            )
            server_ms += resp.get("metrics", {}).get("latencyMs", 0)
            text = _response_text(resp)
            if not self.structured_output:
                text = _strip_fences(text)
            try:
                if resp.get("stopReason") == "max_tokens":
                    raise ValueError("response truncated at maxTokens")
                result = ExtractionResult.model_validate_json(text)
            except (ValidationError, ValueError) as e:
                last_error = str(e)
                messages = [
                    *messages,
                    {"role": "assistant", "content": [{"text": text or "(empty)"}]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": f"That output was invalid: {last_error}\n"
                                "Return corrected JSON only."
                            }
                        ],
                    },
                ]
                continue
            return ExtractionOutcome(
                result=result,
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                attempts=attempt,
                usage=usage,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                bedrock_latency_ms=server_ms,
                structured_output=self.structured_output,
            )
        raise ExtractionError(
            f"invalid model output after {self.MAX_ATTEMPTS} attempts: {last_error}"
        )
