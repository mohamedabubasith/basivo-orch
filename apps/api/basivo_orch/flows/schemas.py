"""Request and response shapes for the flow API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import NodeStatus, RunStatus, TriggerKind

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class FlowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=160, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=2000)
    graph: Graph = Field(default_factory=Graph)


class FlowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    graph: Graph | None = None


class TemplateInstall(BaseModel):
    """What a template needs before its first message arrives."""

    name: str | None = Field(default=None, max_length=160)
    telegram_credential_id: str = Field(default="", max_length=64)
    llm_credential_id: str = Field(default="", max_length=64)
    #: Left blank, install asks the credential's provider what models the
    #: account has and takes the first.
    llm_model: str = Field(default="", max_length=160)


class TelegramConnect(BaseModel):
    credential_id: str = Field(min_length=1, max_length=64)
    #: Discard whatever queued up while the bot was pointed elsewhere. On by
    #: default: those updates were meant for another deployment, and replaying
    #: them here renders videos nobody asked for.
    drop_pending: bool = True


class FlowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    published_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_published(self) -> bool:
        return self.published_version_id is not None


class FlowSummary(FlowRead):
    """A flow as the list needs it, not as the table stores it.

    The list used to show a name, a slug and "updated 5 days ago" — none of
    which answers the questions you open that page with: what starts this,
    how big is it, and did the last run pass. Those come from the published
    (or latest) version's graph and the most recent run, computed once for the
    whole page rather than by the browser asking per row.
    """

    node_count: int = 0
    trigger_type: str | None = None
    last_run_status: str | None = None
    last_run_at: datetime | None = None
    #: When this flow next fires, if it is scheduled and published.
    next_run_at: datetime | None = None


class FlowDetail(FlowRead):
    graph: Graph
    version: int

    #: When the scheduler will fire this flow next, if it is scheduled and
    #: published. Null otherwise. Shown in the editor so "is my cron actually
    #: armed?" is answerable without reading the database.
    next_run_at: datetime | None = None


class GraphProblems(BaseModel):
    """Returned as 422 when a graph will not execute."""

    detail: str = "This flow cannot run yet."
    problems: list[str]


class RunRequest(BaseModel):
    """Body of a run request.

    `input` is whatever the flow's trigger expects, and is addressable in node
    config as `{{ trigger.payload.* }}`.
    """

    input: dict[str, Any] = Field(default_factory=dict)
    #: Repeat calls with the same key return the original run instead of
    #: starting a second one. Webhook providers retry on timeout, and without
    #: this a slow flow gets executed twice for one real event.
    idempotency_key: str | None = Field(default=None, max_length=200)


class NodeExecutionRead(BaseModel):
    """The SOW section 3 log record, as the API returns it."""

    model_config = ConfigDict(from_attributes=True)

    node_id: str
    node_type: str
    node_name: str | None
    status: NodeStatus
    attempt: int
    input_summary: dict[str, Any] | None
    output_summary: dict[str, Any] | None
    error: str | None
    duration_ms: int | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    started_at: datetime
    finished_at: datetime | None


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    flow_id: uuid.UUID
    flow_version_id: uuid.UUID
    status: RunStatus
    trigger: TriggerKind
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class RunDetail(RunRead):
    nodes: list[NodeExecutionRead] = Field(default_factory=list)


class RunAccepted(BaseModel):
    """202 response for the async variant."""

    run_id: uuid.UUID
    status: RunStatus
    #: Where to poll, and where to attach a live stream. Returned so a caller
    #: never has to construct URLs from string fragments in their own code.
    poll_url: str
    stream_url: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyRead):
    """The only response that ever contains the key itself."""

    key: str = Field(description="Shown once. Only a hash is stored.")


class NodeTypeRead(BaseModel):
    type: str
    label: str
    description: str
    tier: int
    category: str
    is_trigger: bool
    #: The guide behind the palette's info button. Same warning as
    #: output_paths: absent here, FastAPI strips them and the guide is blank.
    when: str
    needs: list[str]
    example: str
    ports: list[str]
    #: Suggestable paths into this node's output — the editor's template
    #: autocomplete is built from these. Must be listed here or FastAPI's
    #: response_model filtering silently strips them from the palette.
    output_paths: list[str]
    config_schema: dict[str, Any]


def slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "flow"


class _SlugCheck(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)

    @field_validator("slug")
    @classmethod
    def _ok(cls, value: str) -> str:
        return value
