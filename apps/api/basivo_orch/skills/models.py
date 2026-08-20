"""Stored skills: procedures an agent can look up instead of being told.

A skill is what you would otherwise paste into the system prompt of every
agent that needs it. Doing that has three costs: the text is duplicated across
flows and drifts, it is spent on every single run whether or not it is
relevant, and a long enough prompt starts to crowd out the actual request.

So skills are separate objects, and an agent is given only their **names and
one-line descriptions**. It reads the full instructions by calling a tool, and
only when it decides one applies. That is the property that makes a library of
thirty skills affordable: thirty descriptions cost a few hundred tokens, and
the run pays for the body of the one skill it actually used.

The shape is deliberately Anthropic's `SKILL.md` — frontmatter `name` and
`description`, then a markdown body, with optional bundled files — so a skill
written for Claude imports here unchanged (see `schemas.parse_skill_md`), and
one written here is portable back out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from basivo_orch.db import Base
from basivo_orch.flows.models import JSONColumn, _uuid_pk


class Skill(Base):
    """One procedure, owned by a workspace."""

    __tablename__ = "skill"
    __table_args__ = (
        # The name is what the model calls the skill by, so it has to be
        # unambiguous within the workspace — two skills called "refunds" would
        # make `load_skill("refunds")` a coin toss.
        UniqueConstraint("organization_id", "name", name="uq_skill_org_name"),
        Index("ix_skill_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    #: Lowercase, hyphenated — a tool argument, not a title.
    name: Mapped[str] = mapped_column(String(80))
    #: **When to use this**, in one line. This is the only part of a skill the
    #: model sees before choosing, so it is the whole basis of that choice: a
    #: description that says what the skill *is* ("refund policy") is much
    #: weaker than one that says when it applies ("use when a customer asks
    #: for money back, including partial refunds and chargebacks").
    description: Mapped[str] = mapped_column(String(500))
    #: The body: the procedure itself, in markdown.
    instructions: Mapped[str] = mapped_column(Text())

    #: Bundled reference files, `[{"name": "...", "content": "..."}]`, read
    #: individually and only when asked for. A 40-page policy belongs here
    #: rather than in `instructions`, which is loaded whole.
    resources: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, default=list)

    #: How many runs have loaded it. The library's own answer to "is this
    #: skill earning its place, or has nothing touched it since March?"
    load_count: Mapped[int] = mapped_column(Integer(), default=0)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
