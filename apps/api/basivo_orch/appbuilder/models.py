"""A built app, its versions, and the conversation that produced it.

The shape mirrors Flows deliberately, because it is the shape people here
already understand: a project is the thing you work on, a version is an
immutable snapshot, and publishing points the project at one of them. Deploy
is therefore a single write, and rolling back is the same write with an older
id, which is what makes "that made it worse" a button rather than a support
request.

**The tree is the state.** `source_artifact_id` on the project is the files as
they stand after the last good turn, and that is what the next turn opens. No
agent session is stored: sessions are opaque, are invalidated by a CLI upgrade,
and would tie a project to whichever engine started it. A coding agent reads
the directory, which is what it is built to do.

Bytes live in `artifact`, beside every other file this product produces, for
the reason written there: the API and the worker are separate containers, and
Postgres is already backed up. A page and its build are hundreds of kilobytes.
Object storage is the upgrade when somebody stores video in one.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from basivo_orch.db import Base
from basivo_orch.flows.models import _uuid_pk


class TurnStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: The agent changed files and the result built.
    BUILT = "built"
    #: The agent worked but the build failed, or it refused the task. The
    #: project keeps its previous tree; a broken app is never stored.
    FAILED = "failed"


class AppProject(Base):
    """One app somebody is building by describing it."""

    __tablename__ = "app_project"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_app_project_org_slug"),
        Index("ix_app_project_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(160))
    #: The published address, unique across every workspace: the name plus
    #: four random characters, so `sunrise-bakery-k3d9` can be read aloud and
    #: still belongs to exactly one app.
    public_slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)

    #: Which coding agent runs the turns. `auto` reads the workspace's
    #: credentials, exactly as the repair node does.
    engine: Mapped[str] = mapped_column(String(32), default="auto")

    #: The flow whose single node runs this project's turns. A project borrows
    #: the run queue, the event log, the retry semantics and the Runs screen
    #: rather than growing a second copy of all four.
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flow.id", ondelete="SET NULL"), default=None
    )

    #: The files as they stand. What the next turn opens.
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact.id", ondelete="SET NULL"), default=None
    )
    #: What a visitor to the share link is served. Null until first deploy,
    #: which is what makes deploying a real gate rather than a flag.
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_version.id", ondelete="SET NULL", use_alter=True), default=None
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list[AppVersion]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        foreign_keys="AppVersion.project_id",
    )


class AppVersion(Base):
    """An immutable build. Every clean turn makes one.

    Both halves are kept: `build_artifact_id` is what a browser loads, and
    `source_artifact_id` is what "go back to this one" restores. Keeping only
    the build would make undo impossible, which is the feature this table is
    really for.
    """

    __tablename__ = "app_version"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_app_version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_project.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer())

    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact.id", ondelete="SET NULL"), default=None
    )
    build_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact.id", ondelete="SET NULL"), default=None
    )
    #: Which agent built it, for the run log and for support. Never shown as a
    #: model name.
    engine: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[AppProject] = relationship(back_populates="versions", foreign_keys=[project_id])


class AppTurn(Base):
    """One message and what came of it.

    This is the conversation the console shows and the short history the next
    turn is given. The agent's tool calls are deliberately not here: they are
    where file contents and third-party payloads live, and a table read by the
    browser is the wrong place for those.
    """

    __tablename__ = "app_turn"
    __table_args__ = (Index("ix_app_turn_project_created", "project_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_project.id", ondelete="CASCADE"), index=True
    )
    #: The run that executed it, so the console can follow the live log and a
    #: person can open the full record on the Runs screen.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run.id", ondelete="SET NULL"), default=None
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_version.id", ondelete="SET NULL"), default=None
    )

    prompt: Mapped[str] = mapped_column(Text())
    #: What the agent said afterwards, in the two or three sentences the rules
    #: ask it for.
    reply: Mapped[str] = mapped_column(Text(), default="")
    #: Why a failed turn failed, in words a person can act on.
    error: Mapped[str] = mapped_column(Text(), default="")
    status: Mapped[TurnStatus] = mapped_column(
        Enum(TurnStatus, native_enum=False, length=16), default=TurnStatus.QUEUED
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
