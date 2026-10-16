"""Runtime configuration, loaded from environment variables (and `.env` in development).

Model IDs, thresholds and secrets live here so they can change without a code change.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MediaSource = Literal["ring_playground", "ring_device", "own_footage"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://dockhand:dockhand@localhost:5432/dockhand"

    # Ring. Device IDs are discovered at runtime from GET /v1/devices, never configured.
    ring_api_base_url: str = "https://api.amazonvision.com"
    ring_oauth_base_url: str = "https://oauth.ring.com"
    ring_access_token: SecretStr | None = None  # Playground one-click token (~30 min lifetime)
    ring_webhook_secret: SecretStr | None = None  # the app's HMAC Signature Key
    media_source: MediaSource = "ring_playground"
    ring_http_timeout_s: float = 20.0

    # Ingest. Which events start the capture pipeline, as `type` or `type.sub_type` in the
    # webhook vocabulary (Event History types are mapped onto it, see app/ingest/events.py).
    ingest_relevant_events: list[str] = ["motion_detected.human", "button_press"]
    # Live capture only makes sense while the event is still happening. A webhook retried by
    # Ring minutes later (or a backlog after downtime) would capture an unrelated scene.
    capture_max_event_age_s: float = 120.0
    queue_workers: int = 2
    queue_max_attempts: int = 3
    # A job claimed longer ago than this is assumed to belong to a crashed worker and is retried.
    queue_lease_s: float = 300.0
    queue_idle_poll_s: float = 1.0
    # Capture (WHEP) and frame selection. See app/ring/whep.py and app/capture/selection.py.
    capture_stall_s: float = 2.0  # no frame for this long = the clip has ended
    capture_timeout_s: float = 25.0  # battery cameras end WHEP sessions after 30 s
    capture_sample_frames: int = 10  # spread evenly over the clip, then scored
    capture_select_frames: int = 3  # sent to the model (SPEC.md: top K, default 3)
    capture_sharpness_weight: float = 0.5  # vs. change from the empty-scene reference
    # Optional `<device_id>.jpg` empty-doorstep references. Without one, the clip median is used.
    capture_reference_dir: Path | None = None
    media_dir: Path = Path("data/media")  # selected frames are kept here as evidence

    # Event History polling fallback (the Playground does not deliver webhooks). 0 disables it.
    ring_poll_interval_s: float = 0.0
    ring_poll_event_types: str = "motion.human,ding"
    # Bearer token for the manual trigger endpoint. Unset disables the endpoint.
    admin_token: SecretStr | None = None

    # Bedrock. Model IDs are cross-region inference profiles; confirm availability with a small
    # Converse call (listing a profile does not mean the account can invoke it). See
    # docs/decisions.md for why the default is Nova Pro.
    aws_region: str = "us-east-1"
    # Bedrock API key (bearer token). If unset, boto3 falls back to the standard AWS credential
    # chain (env, ~/.aws, SSO) and signs with SigV4.
    aws_bearer_token_bedrock: SecretStr | None = None
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"
    # Converse structured output (`outputConfig` JSON schema). Off for models that reject it
    # (e.g. Amazon Nova); the schema then goes in the system prompt and Pydantic still validates.
    bedrock_structured_output: bool = False
    # Second model the extraction eval always runs alongside the default, so every report
    # compares the two on the same labelled set.
    bedrock_comparison_model_id: str = "us.anthropic.claude-sonnet-4-6"
    bedrock_comparison_structured_output: bool = True
    extraction_prompt_version: str = "v1"
    extraction_max_tokens: int = 1024
    bedrock_timeout_s: float = 60.0

    # Reconciliation. See app/reconcile/cost.py for what each term means and
    # docs/decisions.md for how the defaults were chosen.
    reconcile_grace_minutes: float = 60.0
    reconcile_max_cost: float = 1.0
    reconcile_carrier_mismatch_cost: float = 0.5
    reconcile_carrier_unknown_cost: float = 0.2
    reconcile_time_cost_per_hour: float = 0.15
    reconcile_max_offset_hours: float = 8.0
    reconcile_count_cost_weight: float = 0.5

    @field_validator(
        "ring_access_token",
        "ring_webhook_secret",
        "aws_bearer_token_bedrock",
        "admin_token",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_unset(cls, v: object) -> object:
        # `.env.example` ships `KEY=` lines. An empty secret must mean "not configured", or
        # an empty webhook key / admin token would be accepted as valid credentials.
        if isinstance(v, str) and not v.strip():
            return None
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
