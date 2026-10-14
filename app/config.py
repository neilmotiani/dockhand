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
    bedrock_model_id: str = "us.anthropic.claude-sonnet-5-5"
    extraction_prompt_version: str = "v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
