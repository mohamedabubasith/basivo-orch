from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Kept in sync with pydantic-ai's provider registry (`pydantic_ai.providers`).
#: Not imported from there directly — that would make the API schema depend on
#: an internal module layout the library does not promise to keep stable — but
#: every value here must be one `infer_provider()` accepts.
PROVIDERS: list[str] = [
    "anthropic",
    "openai",
    "google",
    "groq",
    "mistral",
    "cohere",
    "bedrock",
    "azure",
    "deepseek",
    "xai",
    "openrouter",
    "together",
    "fireworks",
    "cerebras",
    "huggingface",
    "ollama",
    "moonshotai",
    "zai",
    "sambanova",
    "nebius",
    "ovhcloud",
    "alibaba",
]


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(description="One of PROVIDERS.")
    api_key: str = Field(min_length=1, max_length=4000)
    base_url: str | None = Field(default=None, max_length=300)
    options: dict[str, Any] = Field(default_factory=dict)


class CredentialRead(BaseModel):
    """Never carries the secret. `hint` is the last four characters, for
    telling two credentials apart without exposing either."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: str
    hint: str
    base_url: str | None
    created_at: datetime
    last_used_at: datetime | None
