"""What a saved tool and a saved MCP server look like over HTTP.

The write shape is flat — name, description, kind, and the fields that kind
needs — because that is the form a person fills in. The row keeps the
kind-specific half in one JSON column, so adding a kind later is not a
migration.

Validation is the node's, not a second copy of it: `ToolDefinition` already
knows that an HTTP tool needs a URL and a code tool needs code, and a saved
tool that the node would refuse is a trap that springs at run time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="What the model calls it. Lowercase and underscores.",
    )
    description: str = Field(
        min_length=1,
        max_length=1024,
        description="When to use it. This is the whole basis of the model's decision.",
    )
    kind: Literal["http", "code", "constant"] = "http"
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    # -- http --------------------------------------------------------------
    url: str = Field(default="", max_length=2000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None

    # -- code --------------------------------------------------------------
    code: str = Field(default="", max_length=20_000)
    timeout_seconds: float = Field(default=20.0, ge=1, le=120)

    # -- constant ----------------------------------------------------------
    value: Any = None

    @model_validator(mode="after")
    def _kind_has_what_it_needs(self) -> ToolWrite:
        # The same rule the node enforces, applied where a person can still fix
        # it: a tool saved without its URL fails on somebody else's run.
        if self.kind == "http" and not self.url.strip():
            raise ValueError("An HTTP tool needs a URL.")
        if self.kind == "code" and not self.code.strip():
            raise ValueError("A code tool needs its code.")
        return self

    def to_row(self) -> dict[str, Any]:
        """Split into the columns and the kind-specific rest."""
        data = self.model_dump()
        return {
            "name": data.pop("name"),
            "description": data.pop("description"),
            "kind": data.pop("kind"),
            "definition": data,
        }


class ToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    kind: str
    definition: dict[str, Any]
    updated_at: datetime


class McpWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="A short handle. Its tools reach the model as name__tool.",
    )
    url: str = Field(min_length=8, max_length=500)
    credential_id: str = Field(default="", max_length=64)
    headers: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True

    @model_validator(mode="after")
    def _http_only(self) -> McpWrite:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(
                "MCP servers are reached over http(s). Run a command-line server behind an "
                "HTTP bridge instead."
            )
        return self


class McpRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    url: str
    credential_id: str
    headers: dict[str, str]
    tools: list[str]
    enabled: bool
    updated_at: datetime
