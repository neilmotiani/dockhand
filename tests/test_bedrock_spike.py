from typing import Any

from botocore.exceptions import ClientError
from PIL import Image

from app.config import Settings
from scripts import bedrock_spike


class FakeBedrock:
    def __init__(self, pages: list[list[str]] | None = None, error: str | None = None) -> None:
        self.pages, self.error = pages or [], error

    def get_paginator(self, name: str) -> "FakeBedrock":
        assert name == "list_inference_profiles"
        return self

    def paginate(self, **_: Any):
        if self.error:
            raise ClientError(
                {"Error": {"Code": self.error, "Message": "x"}}, "ListInferenceProfiles"
            )
        for ids in self.pages:
            yield {"inferenceProfileSummaries": [{"inferenceProfileId": i} for i in ids]}


def resolve(
    monkeypatch, fake: FakeBedrock, configured: str = "us.anthropic.claude-sonnet-5-5"
) -> str:
    monkeypatch.setattr(bedrock_spike, "make_bedrock_client", lambda s, svc: fake)
    return bedrock_spike.resolve_model_id(Settings(_env_file=None, bedrock_model_id=configured))


def test_resolve_keeps_configured_when_listed(monkeypatch) -> None:
    fake = FakeBedrock([["us.anthropic.claude-haiku-4-5"], ["us.anthropic.claude-sonnet-5-5"]])
    assert resolve(monkeypatch, fake) == "us.anthropic.claude-sonnet-5-5"


def test_resolve_picks_newest_sonnet_in_same_geo(monkeypatch) -> None:
    fake = FakeBedrock(
        [
            [
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "us.anthropic.claude-sonnet-5-5-20260801-v1:0",
                "eu.anthropic.claude-sonnet-9-0",
                "us.anthropic.claude-opus-5-5",
                "us.anthropic.claude-sonnet-10-0",
            ]
        ]
    )
    assert (
        resolve(monkeypatch, fake, "us.anthropic.claude-sonnet-x")
        == "us.anthropic.claude-sonnet-10-0"
    )


def test_resolve_falls_back_when_none_listed(monkeypatch) -> None:
    assert resolve(monkeypatch, FakeBedrock([["us.amazon.nova-pro-v1:0"]])) == (
        "us.anthropic.claude-sonnet-5-5"
    )


def test_resolve_falls_back_on_access_denied(monkeypatch) -> None:
    assert resolve(monkeypatch, FakeBedrock(error="AccessDeniedException")) == (
        "us.anthropic.claude-sonnet-5-5"
    )


def test_generates_image_when_dir_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bedrock_spike, "SPIKE_DIR", tmp_path / "spike")
    path = bedrock_spike.pick_image()
    assert path.name == "synthetic_ups_3_boxes.png"
    with Image.open(path) as img:
        assert img.size == (1280, 720)


def test_uses_existing_image(monkeypatch, tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("x")
    Image.new("RGB", (4, 4)).save(tmp_path / "b.jpg")
    Image.new("RGB", (4, 4)).save(tmp_path / "a.png")
    monkeypatch.setattr(bedrock_spike, "SPIKE_DIR", tmp_path)
    assert bedrock_spike.pick_image().name == "a.png"
