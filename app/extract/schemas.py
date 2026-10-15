"""Structured output of the vision extraction step (SPEC.md section 5)."""

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Carrier(StrEnum):
    UPS = "UPS"
    FEDEX = "FedEx"
    USPS = "USPS"
    AMAZON = "Amazon"
    DHL = "DHL"
    SYSCO = "Sysco"
    US_FOODS = "US Foods"
    OTHER = "Other"
    UNKNOWN = "Unknown"


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `reasoning` comes first so the model writes its observations before committing to the
    # answer fields (output is generated in schema order).
    reasoning: str
    carrier: Carrier
    package_count: int = Field(ge=0)
    label_text: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


# JSON schema sent to Bedrock structured output. Hand-written instead of
# ExtractionResult.model_json_schema() because constrained decoding supports only a subset of
# JSON Schema (no numeric bounds, no $defs). Range checks are enforced by Pydantic afterwards.
EXTRACTION_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reasoning", "carrier", "package_count", "label_text", "confidence"],
    "properties": {
        "reasoning": {"type": "string"},
        "carrier": {"type": "string", "enum": [c.value for c in Carrier]},
        "package_count": {"type": "integer"},
        "label_text": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}

EXTRACTION_JSON_SCHEMA_STR = json.dumps(EXTRACTION_JSON_SCHEMA)
