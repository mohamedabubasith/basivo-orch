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
    # VCS hosts, for the git.ticket / git.autofix nodes — not model providers.
    "github",
    "gitlab",
    # Jira Cloud: a ticket source for the webhook trigger, and where Fix Code
    # and Open PR reports back. Key is written email:api-token; base URL is the site.
    "jira",
    # A bearer token for an MCP server an agent calls (see flows/nodes/mcp.py).
    "mcp",
    "telegram",
    "discord",
    "slack",
    "mastodon",
    "bluesky",
]


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(description="One of PROVIDERS.")
    api_key: str = Field(min_length=1, max_length=4000)
    base_url: str | None = Field(default=None, max_length=300)
    options: dict[str, Any] = Field(default_factory=dict)


class CredentialUpdate(BaseModel):
    """What may change after creation. Every field optional; an omitted or
    empty `api_key` keeps the stored secret, so renaming never means
    re-pasting a key."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=4000)
    base_url: str | None = Field(default=None, max_length=300)
    options: dict[str, Any] | None = None


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


class CredentialTestRequest(BaseModel):
    """An unsaved candidate key — the "Test connection" button fires this
    before anything is persisted, so a typo is caught before it is stored."""

    provider: str
    api_key: str = Field(min_length=1, max_length=4000)
    base_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ModelListResponse(BaseModel):
    """Whether this provider's catalog can be fetched live, and what it holds.

    `supported=False` is not an error — Bedrock, Hugging Face, Azure, Mistral
    and Cohere have no practical live fetch here (see `model_catalog`'s
    docstring), and the UI's answer to that is a free-text model field, not a
    scary message. `error` is set only when a *supported* provider's fetch
    itself failed, almost always because the key is wrong.
    """

    supported: bool
    models: list[str] = Field(default_factory=list)
    error: str | None = None


class RepoListResponse(BaseModel):
    """Repositories a code-hosting credential can open, for the Repo picker."""

    supported: bool
    repos: list[str] = Field(default_factory=list)
    error: str | None = None
