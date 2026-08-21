"""Application settings for the orchestrator itself.

Auth carries its own settings module (``basivo_orch.auth.config``) reading the
same ``.env``. The split is deliberate: auth settings are security-critical and
validated far more strictly, and keeping them separate means a change here can
never relax a control there.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "test" is included because the auth package's test suite sets
    # ENVIRONMENT=test to relax cookie and TLS requirements; without it here,
    # importing this module during those tests fails validation.
    ENVIRONMENT: Literal["development", "test", "staging", "production"] = "development"
    DEBUG: bool = False

    APP_NAME: str = "Basivo Orchestrator"
    API_V1_PREFIX: str = "/api/v1"

    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://basivo:basivo@localhost:5432/basivo_orch",
        description="Async SQLAlchemy URL. Shared with the auth package.",
    )
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_RECYCLE_SECONDS: int = 1800
    DATABASE_ECHO: bool = False

    REQUIRE_VERIFIED_EMAIL: bool = Field(
        default=True,
        description=(
            "Refuse workspace access until the account's email is confirmed. "
            "This is the real gate. The UI only mirrors it. Turning it off is "
            "an escape hatch for a deployment whose mail is not yet delivering: "
            "with it on and mail broken, nobody who signs up can ever get in."
        ),
    )

    @field_validator("DEBUG")
    @classmethod
    def _no_debug_in_production(cls, value: bool, info) -> bool:  # type: ignore[no-untyped-def]
        if value and info.data.get("ENVIRONMENT") == "production":
            raise ValueError("DEBUG must be false when ENVIRONMENT=production")
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
