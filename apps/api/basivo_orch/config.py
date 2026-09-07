"""Application settings for the orchestrator itself.

Auth carries its own settings module (``basivo_orch.auth.config``) reading the
same ``.env``. The split is deliberate: auth settings are security-critical and
validated far more strictly, and keeping them separate means a change here can
never relax a control there.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    # --- billing ----------------------------------------------------------
    # One switch decides whether money is real in this deployment.
    #
    #   demo        the plans are a preview. Nothing is enforced, no limit
    #               applies, the provider is never called and the webhook
    #               endpoint does not exist. A demo box cannot take a payment
    #               even if someone points a live provider at it.
    #   production  the free plan and its limits apply to every workspace, and
    #               paying moves a workspace onto a bigger one.
    #
    # It is deliberately not derived from ENVIRONMENT: a staging box runs
    # ENVIRONMENT=production for cookie and TLS strictness while still having
    # no payment provider behind it.
    BILLING_MODE: Literal["demo", "production"] = "demo"

    DODO_API_KEY: str = ""
    DODO_WEBHOOK_SECRET: str = ""
    DODO_ENVIRONMENT: Literal["test_mode", "live_mode"] = "test_mode"
    #: Provider product ids, one per paid plan. Both are required in
    #: production mode: a plan whose product id is missing would send a
    #: customer to a checkout for nothing.
    DODO_PRODUCT_PRO: str = ""
    DODO_PRODUCT_TEAM: str = ""
    #: How long a workspace keeps its plan after a payment fails. The
    #: subscription is not cancelled and nothing is deleted; the limits simply
    #: fall back to Free once this passes.
    BILLING_GRACE_DAYS: int = 7

    @property
    def billing_is_live(self) -> bool:
        return self.BILLING_MODE == "production"

    @model_validator(mode="after")
    def _production_billing_is_configured(self) -> Settings:
        """Fail at startup, not at the first checkout.

        A deployment that says it takes money and has no key would look
        healthy right up to the moment a customer tried to pay.
        """
        if self.BILLING_MODE != "production":
            return self
        missing = [
            name
            for name, value in (
                ("DODO_API_KEY", self.DODO_API_KEY),
                ("DODO_WEBHOOK_SECRET", self.DODO_WEBHOOK_SECRET),
                ("DODO_PRODUCT_PRO", self.DODO_PRODUCT_PRO),
                ("DODO_PRODUCT_TEAM", self.DODO_PRODUCT_TEAM),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError("BILLING_MODE=production needs " + ", ".join(missing) + " to be set.")
        return self

    def product_id(self, plan: str) -> str:
        """The provider product for a plan code, or empty when there is none."""
        return {
            "pro": self.DODO_PRODUCT_PRO.strip(),
            "team": self.DODO_PRODUCT_TEAM.strip(),
        }.get(plan, "")

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
