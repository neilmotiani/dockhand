"""Spike: one image through Bedrock Converse with the extraction schema.

Resolves the model ID (ListInferenceProfiles, falling back to BEDROCK_MODEL_ID), sends the first
image in data/spike/ (generating a synthetic doorstep image if the folder is empty), validates
the output with Pydantic and prints the result, token usage and latency.

Credentials are never printed. Do not turn on botocore debug logging here: it logs request
headers, which would include the bearer token.

Usage:
    uv run python -m scripts.bedrock_spike
"""

import re
import sys
from pathlib import Path

from botocore.exceptions import ClientError

from app.config import Settings
from app.extract.client import BedrockExtractor, ExtractionError, make_bedrock_client

SPIKE_DIR = Path("data/spike")
FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}


def generate_test_image(path: Path) -> None:
    """A doorstep with 3 boxes, one with a UPS label, plus a fake camera watermark the model
    should ignore. Expected answer: UPS, 3 packages."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1280, 720), (150, 150, 145))
    d = ImageDraw.Draw(img)
    d.rectangle([440, 40, 840, 560], fill=(90, 60, 40), outline=(40, 30, 20), width=6)  # door
    d.rectangle([0, 560, 1280, 720], fill=(110, 110, 105))  # step
    font = ImageFont.load_default(size=28)
    small = ImageFont.load_default(size=18)
    boxes = [(300, 450, 520, 620), (560, 480, 740, 630), (780, 430, 1000, 620)]
    for x0, y0, x1, y1 in boxes:
        d.rectangle([x0, y0, x1, y1], fill=(196, 154, 108), outline=(120, 90, 60), width=4)
        d.line([(x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2)], fill=(170, 130, 90), width=6)
    x0, y0 = 320, 470  # shipping label on the first box
    d.rectangle([x0, y0, x0 + 180, y0 + 110], fill=(250, 250, 250), outline=(0, 0, 0), width=2)
    d.rectangle([x0 + 8, y0 + 8, x0 + 70, y0 + 48], fill=(60, 40, 20))
    d.text((x0 + 14, y0 + 12), "UPS", fill=(255, 190, 0), font=font)
    d.text((x0 + 8, y0 + 56), "1Z 999 AA1 01", fill=(0, 0, 0), font=small)
    d.text((x0 + 8, y0 + 80), "23 4567 84", fill=(0, 0, 0), font=small)
    d.text(
        (20, 680),
        "ring  Back Door  2026-10-15 10:42:07  dev 3f9a1c",
        fill=(255, 255, 255),
        font=small,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def pick_image() -> Path:
    images = sorted(p for p in SPIKE_DIR.glob("*") if p.suffix.lower() in FORMATS)
    if images:
        return images[0]
    path = SPIKE_DIR / "synthetic_ups_3_boxes.png"
    generate_test_image(path)
    print(f"data/spike/ was empty; generated {path} (expected: UPS, 3 packages)")
    return path


def _version_key(profile_id: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", profile_id)]


def resolve_model_id(settings: Settings) -> str:
    """Use the configured ID if Bedrock lists it; otherwise the newest listed Sonnet profile for
    this geography. If listing is not permitted, fall back to the configured ID."""
    configured = settings.bedrock_model_id
    try:
        client = make_bedrock_client(settings, "bedrock")
        ids: list[str] = []
        for page in client.get_paginator("list_inference_profiles").paginate(
            typeEquals="SYSTEM_DEFINED"
        ):
            ids += [p["inferenceProfileId"] for p in page["inferenceProfileSummaries"]]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"ListInferenceProfiles failed ({code}); using configured {configured}")
        return configured
    if configured in ids:
        print(f"model {configured} confirmed by ListInferenceProfiles")
        return configured
    geo = configured.split(".", 1)[0] + "."
    sonnets = sorted(
        (i for i in ids if i.startswith(f"{geo}anthropic.claude-sonnet")), key=_version_key
    )
    if not sonnets:
        print(f"no Sonnet profile listed; trying configured {configured}")
        return configured
    print(
        f"configured {configured} not listed; using newest Sonnet {sonnets[-1]}. "
        "Set BEDROCK_MODEL_ID to keep it."
    )
    return sonnets[-1]


def main() -> int:
    settings = Settings()
    print(
        f"region={settings.aws_region} auth="
        f"{'bedrock api key' if settings.aws_bearer_token_bedrock else 'aws credential chain'} "
        f"prompt={settings.extraction_prompt_version}"
    )
    model_id = resolve_model_id(settings)
    settings = settings.model_copy(update={"bedrock_model_id": model_id})

    path = pick_image()
    extractor = BedrockExtractor(make_bedrock_client(settings), settings)
    try:
        out = extractor.extract([(path.read_bytes(), FORMATS[path.suffix.lower()])])
    except ClientError as e:
        err = e.response["Error"]
        print(f"Converse failed: {err['Code']}: {err['Message']}", file=sys.stderr)
        if "CallWithBearerToken" in err["Message"]:
            print(
                "The account blocks Bedrock API keys. Use SigV4 credentials instead (IAM access "
                "key or `aws configure sso`) and unset AWS_BEARER_TOKEN_BEDROCK.",
                file=sys.stderr,
            )
        return 1
    except ExtractionError as e:
        print(f"extraction failed: {e}", file=sys.stderr)
        return 1

    print(f"image:   {path}")
    print(f"model:   {out.model_id} (prompt {out.prompt_version}, attempts {out.attempts})")
    print("result:  " + out.result.model_dump_json(indent=2).replace("\n", "\n         "))
    print(
        f"usage:   input {out.usage.input_tokens}, output {out.usage.output_tokens}, "
        f"total {out.usage.total_tokens} tokens"
    )
    print(f"latency: {out.latency_ms} ms wall, {out.bedrock_latency_ms} ms reported by Bedrock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
