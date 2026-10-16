"""Runtime configuration, loaded from environment variables (and `.env` in development).

Model IDs, thresholds and secrets live here so they can change without a code change.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
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
    ring_webhook_secret: SecretStr | None = None
    media_source: MediaSource = "ring_playground"

    # Bedrock. The default is the cross-region inference profile for the current Sonnet;
    # confirm with `aws bedrock list-inference-profiles --region us-east-1`.
    aws_region: str = "us-east-1"
    # Bedrock API key (bearer token). If unset, boto3 falls back to the standard AWS credential
    # chain (env, ~/.aws, SSO) and signs with SigV4.
    aws_bearer_token_bedrock: SecretStr | None = None
    bedrock_model_id: str = "us.anthropic.claude-sonnet-5-5"
    # Converse structured output (`outputConfig` JSON schema). Turn off for models that reject it
    # (e.g. Amazon Nova); the schema then goes in the system prompt and Pydantic still validates.
    bedrock_structured_output: bool = True
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
