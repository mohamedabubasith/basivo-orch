"""Tools and MCP servers a workspace owns, rather than a node does.

A tool used to live inside the agent node that called it. That is the right
place for exactly one agent: the moment a second one needs the same "look up
the order" call, the definition is copied, and from then on the two drift —
one gets the bug fix, the other does not, and nobody can answer "which flows
call our orders API" without opening every node on every canvas.

So a tool is a workspace object with a name, and an agent refers to it. Edit it
once and every agent using it changes. The same argument applies twice over to
MCP servers, which carry a URL and a credential: those belong in one place, not
copied into six graphs where rotating the key means finding all six.

The node's own inline tools still work and are still the right shape for a
one-off — the shared library is for the ones worth naming.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from basivo_orch.db import Base
from basivo_orch.flows.models import JSONColumn, _uuid_pk


class Tool(Base):
    """One tool, defined once and callable by any agent in the workspace."""

    __tablename__ = "agent_tool"
    __table_args__ = (
        # The name is what the model calls it by, so two tools called
        # `get_order` in one workspace would make the choice a coin toss.
        UniqueConstraint("organization_id", "name", name="uq_tool_org_name"),
        Index("ix_tool_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    #: The identifier the model calls: lowercase, underscores, no spaces.
    name: Mapped[str] = mapped_column(String(64))
    #: What the model reads when deciding whether to call it. This is the
    #: whole basis of that decision, so it is required rather than optional.
    description: Mapped[str] = mapped_column(Text(), default="")

    #: "http", "code" or "constant" — the same kinds the node supports, because
    #: a shared tool and an inline one must behave identically.
    kind: Mapped[str] = mapped_column(String(16), default="http")
    #: The rest of the definition, exactly as `ToolDefinition` expects it:
    #: url, method, headers, body, code, value, input_schema.
    definition: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class McpConnection(Base):
    """One MCP server, with its credential, shared across flows."""

    __tablename__ = "mcp_connection"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_mcp_org_name"),
        Index("ix_mcp_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    #: A short handle. Its tools reach the model as `name__tool`, which is what
    #: keeps two servers offering `search` apart.
    name: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(500))
    #: Optional. Sent as `Authorization: Bearer <key>`, resolved at run time so
    #: the secret is never in a graph or in this row.
    credential_id: Mapped[str] = mapped_column(String(64), default="")
    headers: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)
    #: Empty means every tool the server offers.
    tools: Mapped[list[Any]] = mapped_column(JSONColumn, default=list)

    #: A server that is failing, or costing too much, can be switched off in
    #: one place rather than removed from every flow that uses it.
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
