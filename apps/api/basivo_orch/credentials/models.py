"""Stored provider credentials.

The alternative — an API key typed into each agent node — puts a live secret
into the flow graph, and the graph is plain JSON in the database, returned by
`GET /flows/{id}` to anyone who can read the flow, and copied wholesale every
time the flow is versioned. One key would end up in a dozen rows and no one
would know which.

So a credential is its own object: named once, encrypted at rest, referenced by
id, and never returned. Nodes carry a `credential_id`; the secret is resolved
inside the executor and goes nowhere else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from basivo_orch.db import Base
from basivo_orch.flows.models import JSONColumn, _uuid_pk


class Credential(Base):
    """One provider credential, owned by a workspace."""

    __tablename__ = "credential"
    __table_args__ = (
        # Names are how a person picks one in a dropdown; two identically named
        # credentials in one workspace make that choice meaningless.
        UniqueConstraint("organization_id", "name", name="uq_credential_org_name"),
        Index("ix_credential_org_provider", "organization_id", "provider"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(120))
    #: A pydantic-ai provider name: "anthropic", "openai", "google", "groq"…
    provider: Mapped[str] = mapped_column(String(48), index=True)

    #: AES-GCM ciphertext. Never selected into a response schema.
    secret_encrypted: Mapped[str] = mapped_column(Text())

    #: Last four characters of the key, for recognising which one this is
    #: without revealing it. Four is enough to disambiguate and useless alone.
    hint: Mapped[str] = mapped_column(String(8), default="")

    #: Self-hosted gateways, Azure deployments, Ollama, proxies.
    base_url: Mapped[str | None] = mapped_column(String(300), default=None)

    #: Provider-specific extras that are not the secret — an Azure API version,
    #: a Bedrock region. Readable; nothing sensitive belongs here.
    options: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
