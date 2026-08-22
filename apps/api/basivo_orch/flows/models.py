"""Tables for flows, runs, and the observability record.

Two design points that the rest of the package depends on:

**`RunEvent` is the durable event log, not a convenience cache.** Section 4 of
the SOW requires that a caller can start a run over plain HTTP and later attach
to its live stream. That is only possible if events are persisted with a
monotonic sequence per run — a pub/sub fan-out alone would leave an attacher at
t+5s with no idea what happened in the first five seconds. Redis carries the
live tail; this table carries the history, and the SSE endpoint stitches them.

**`NodeExecution` matches the SOW's log shape exactly.** Section 3 names the
fields, and the analysis layer (failure clustering, latency and cost hotspots)
is only as good as what execution records. Columns, not a JSON blob, because
"which node type fails most on inputs mentioning migrations" has to be a query.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from basivo_orch.db import Base

#: JSON that becomes JSONB on Postgres (indexable, binary) and plain JSON on
#: SQLite, which the test suite uses.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class RunStatus(enum.StrEnum):
    """Lifecycle of a whole run. Terminal states end the event stream."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


class NodeStatus(enum.StrEnum):
    """Lifecycle of a single node execution.

    `SKIPPED` is distinct from `SUCCEEDED` on purpose: a branch not taken must
    not be counted as a success when the analysis layer computes per-node
    failure rates, or every Condition node makes its dead branch look healthy.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class TriggerKind(enum.StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    API = "api"


class Flow(Base):
    """A workflow. The graph itself lives in `FlowVersion`."""

    __tablename__ = "flow"
    __table_args__ = (
        # Slugs are addressable, so they must be unique per tenant, not global.
        UniqueConstraint("organization_id", "slug", name="uq_flow_org_slug"),
        Index("ix_flow_org_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text(), default=None)

    #: Which version external callers execute. Null until first publish, which
    #: is what makes "published" a real gate rather than a flag someone forgot.
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flow_version.id", ondelete="SET NULL", use_alter=True),
        default=None,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list[FlowVersion]] = relationship(
        back_populates="flow",
        cascade="all, delete-orphan",
        foreign_keys="FlowVersion.flow_id",
    )


class FlowVersion(Base):
    """An immutable snapshot of a graph.

    Versions are never edited. A run records the exact version it executed, so
    the log of a run from three weeks ago still describes the graph that
    actually ran rather than whatever the flow looks like today.
    """

    __tablename__ = "flow_version"
    __table_args__ = (UniqueConstraint("flow_id", "version", name="uq_flow_version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flow.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer())
    graph: Mapped[dict[str, Any]] = mapped_column(JSONColumn)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    flow: Mapped[Flow] = relationship(back_populates="versions", foreign_keys=[flow_id])


class Run(Base):
    """One execution of one flow version."""

    __tablename__ = "run"
    __table_args__ = (
        Index("ix_run_flow_created", "flow_id", "created_at"),
        Index("ix_run_org_status", "organization_id", "status"),
        # The queue index. Every worker poll is "oldest queued run", several
        # times a second across every worker — it must never be a table scan.
        Index("ix_run_queue", "status", "created_at"),
        # Repeat deliveries are normal for webhooks; this makes retrying safe.
        UniqueConstraint("flow_id", "idempotency_key", name="uq_run_idempotency"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flow.id", ondelete="CASCADE"), index=True
    )
    flow_version_id: Mapped[uuid.UUID] = mapped_column(
        # CASCADE, not RESTRICT. A run only loses its version when the whole
        # flow is deleted — versions are append-only otherwise — and RESTRICT
        # made that deletion impossible for any flow that had ever run: the
        # ORM deletes versions first (the `versions` relationship cascades),
        # runs still referenced them, and every flow delete 500'd. Run history
        # is meaningless without its flow, so it goes with it.
        ForeignKey("flow_version.id", ondelete="CASCADE")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False, length=16), default=RunStatus.QUEUED, index=True
    )
    trigger: Mapped[TriggerKind] = mapped_column(Enum(TriggerKind, native_enum=False, length=16))

    input: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, default=None)
    error: Mapped[str | None] = mapped_column(Text(), default=None)

    idempotency_key: Mapped[str | None] = mapped_column(String(200), default=None)
    #: Who or what started it. Null for API-key triggered runs.
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer(), default=None)

    # --- worker lease -------------------------------------------------------
    #
    # A QUEUED row is a job nobody has taken; these three columns are how a
    # worker takes it and how we find out it died holding it. Without them a
    # worker that is OOM-killed mid-run leaves the row RUNNING forever, and
    # the UI shows a spinner for a run that no process is executing.

    #: Which worker claimed it. Null means it was executed inline by an API
    #: request (`mode=sync`), which the reaper judges by a different clock.
    worker_id: Mapped[str | None] = mapped_column(String(64), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Refreshed while the run executes. A stale heartbeat is the only reliable
    #: evidence that the process holding this run is gone.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    node_executions: Mapped[list[NodeExecution]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="NodeExecution.started_at"
    )


class NodeExecution(Base):
    """The per-node record required by SOW section 3.

    One row per *attempt*, not per node: a node that succeeds on retry 2 must
    not look identical to one that succeeded first time, or the reliability
    numbers the analysis layer produces are wrong.
    """

    __tablename__ = "node_execution"
    __table_args__ = (
        Index("ix_node_exec_run", "run_id", "started_at"),
        # Drives "which node type fails most, and how slowly".
        Index("ix_node_exec_type_status", "node_type", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)

    #: The node's id *within the graph*, not a foreign key.
    node_id: Mapped[str] = mapped_column(String(80))
    node_type: Mapped[str] = mapped_column(String(80), index=True)
    node_name: Mapped[str | None] = mapped_column(String(160), default=None)

    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus, native_enum=False, length=16))
    attempt: Mapped[int] = mapped_column(Integer(), default=1)

    # Summaries, not full payloads. A node's input can be a 10MB document, and
    # storing every one of them turns the log table into the largest thing in
    # the database within a week. Full payloads belong in object storage, keyed
    # from here, if they are ever needed.
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, default=None)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, default=None)

    error: Mapped[str | None] = mapped_column(Text(), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer(), default=None)

    #: Reserved for the analysis layer's cost breakdown. Populated by capability
    #: nodes (Tier 2); Tier 1 nodes leave it null.
    cost_usd: Mapped[float | None] = mapped_column(Float(), default=None)
    tokens_in: Mapped[int | None] = mapped_column(Integer(), default=None)
    tokens_out: Mapped[int | None] = mapped_column(Integer(), default=None)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    run: Mapped[Run] = relationship(back_populates="node_executions")


class RunEvent(Base):
    """Append-only event log for a run. The backbone of cross-mode attach.

    `seq` is per-run and gapless, assigned by the single executor that owns the
    run. A client reconnecting sends `Last-Event-ID: <seq>` and receives exactly
    what it missed — which is what makes an SSE stream survive a dropped
    connection instead of silently losing the middle of a run.
    """

    __tablename__ = "run_event"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer())
    type: Mapped[str] = mapped_column(String(48))
    data: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """Credential for calling published flows from another backend.

    Sessions cannot serve section 4's request/response mode: a cron job or a
    Lambda has no cookie jar and no way to complete a 2FA challenge. Keys are
    organisation-scoped so a leaked key exposes one tenant, and stored only as
    a hash so a database dump does not yield working credentials.
    """

    __tablename__ = "api_key"
    __table_args__ = (Index("ix_api_key_org_active", "organization_id", "revoked_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))

    #: First few characters, stored in clear so the UI can show which key is
    #: which without being able to reconstruct one.
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_usable(self) -> bool:
        from datetime import UTC

        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= datetime.now(UTC):
            return False
        return True


class FlowSchedule(Base):
    """When a scheduled flow fires next. One row per scheduled flow.

    Separate from `flow` because it is written on a completely different
    rhythm: the scheduler updates `next_run_at` every time a flow fires, and
    putting that churn on the flow row would bump `updated_at` on every tick
    and make "last edited" meaningless in the UI.

    The row exists only while the *published* version has a schedule trigger.
    Publishing a version without one deletes it, which is what makes "remove
    the trigger and republish" actually stop the schedule.
    """

    __tablename__ = "flow_schedule"
    __table_args__ = (Index("ix_flow_schedule_due", "next_run_at"),)

    #: The flow is the identity: a flow has at most one schedule.
    flow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("flow.id", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )

    #: The claim marker. The scheduler selects rows due at or before now and
    #: pushes this forward in the same transaction, so a second worker (or a
    #: second tick that overlaps a slow one) cannot fire the same slot twice.
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    #: Copied from the trigger config so the ticker can compute the next slot
    #: without loading and parsing the whole graph for every due flow.
    mode: Mapped[str] = mapped_column(String(16))
    cron: Mapped[str | None] = mapped_column(String(120), default=None)
    interval_seconds: Mapped[int | None] = mapped_column(Integer(), default=None)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    """A file a run produced — a rendered poster, an export, a screenshot.

    The engine passes JSON between nodes, and a poster is bytes. Rather than
    stuff base64 through the graph (which would land in every run's stored
    input/output summary and bloat the log beyond reading), a node saves the
    bytes here and passes the id. Downstream nodes ask the engine for them.

    Stored in the database on purpose. The API and the worker are separate
    processes — separate containers in production — so a local temp file is
    not shared, and requiring object storage to render one poster would put an
    S3 bucket between a user and their first result. Postgres already holds
    the run log and is already backed up; a poster is a few hundred kilobytes.
    `MAX_ARTIFACT_BYTES` is what keeps that honest, and object storage is the
    upgrade when someone stores video.
    """

    __tablename__ = "artifact"
    __table_args__ = (Index("ix_artifact_org_created", "organization_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    #: Cascades, so deleting a flow's runs takes their files with them rather
    #: than leaving orphans nobody can find or attribute.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), default=None, index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(120), default=None)

    filename: Mapped[str] = mapped_column(String(200), default="file")
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer(), default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentMemory(Base):
    """What an agent remembers between runs.

    Without this every run starts blank: a support agent cannot recall the
    customer it spoke to yesterday, and a repair agent re-proposes the fix that
    was already rejected. Memory is keyed by whatever identifies the *subject*
    of the conversation — an issue number, a customer id, a channel — so one
    agent node holds many independent conversations rather than one muddled
    stream.

    **Only the human turn and the final reply are stored.** Tool calls and
    their results are deliberately excluded: they are where credentials, file
    contents and third-party payloads live, and a memory table is exactly the
    wrong place to accumulate those. What a person means by "remember the
    conversation" is what was said, not how it was found out.

    Stored here rather than in a managed memory service because this is the
    most sensitive data an agent touches, and it already sits beside the runs
    and credentials it relates to.
    """

    __tablename__ = "agent_memory"
    __table_args__ = (
        # One row per (workspace, node, subject). The unique constraint is what
        # makes "load then save" safe without a transaction dance.
        UniqueConstraint("organization_id", "scope", "subject", name="uq_agent_memory_scope"),
        Index("ix_agent_memory_updated", "organization_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    #: Which agent owns it — flow id plus node id. Two nodes in one flow keep
    #: separate memories, which is what "this agent remembers" has to mean.
    scope: Mapped[str] = mapped_column(String(200))
    #: Who or what the conversation is about, rendered from the node's config.
    subject: Mapped[str] = mapped_column(String(300))

    #: [{"role": "user"|"assistant", "text": "..."}], oldest first.
    turns: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BotSession(Base):
    """One conversation between an operator and a bot, across many runs.

    The engine runs a DAG once and stops, and it rejects cycles — so "keep
    changing it until the customer is happy" cannot be a loop in the graph. It
    is this row instead: every inbound message starts its own short run, reads
    the state, does one thing, writes the state back, and replies.

    That is not a workaround. A long-running run waiting on a human would hold
    a worker for hours, lose everything if the process restarted, and give no
    honest answer to "what is it doing right now". A session row survives a
    deploy, and each interaction gets its own run log.
    """

    __tablename__ = "bot_session"
    __table_args__ = (
        # One live conversation per chat per bot. The operator's phone is the
        # identity; there is no login on this surface.
        UniqueConstraint("flow_id", "chat_id", name="uq_bot_session_chat"),
        Index("ix_bot_session_sweep", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("flow.id", ondelete="CASCADE"))
    #: Telegram's chat id. A string because group ids are negative, channel ids
    #: are large, and one day this table will hold WhatsApp too.
    chat_id: Mapped[str] = mapped_column(String(64))

    #: COLLECTING → READY → RENDERING → REVIEW → DELIVERED.
    state: Mapped[str] = mapped_column(String(32), default="COLLECTING")

    #: [{"artifact_id", "file_unique_id", "caption"}], in the order sent.
    #: `file_unique_id` is Telegram's stable id for the same picture, which is
    #: how the same photo forwarded twice is not collected twice.
    photos: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, default=list)
    #: What the operator asked for, and the settings behind it.
    brief: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)

    #: The one message the pipeline keeps editing, rather than a wall of
    #: "now doing X" messages nobody can scroll past.
    status_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_video_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact.id", ondelete="SET NULL"), nullable=True
    )
    #: How many videos this job has produced. The cap on it is what stops one
    #: indecisive afternoon from costing a month of compute.
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    spend_usd: Mapped[float] = mapped_column(Float, default=0.0)

    #: Held while a render is in flight. A timestamp rather than a boolean, so
    #: a worker killed mid-render releases the lock by expiry instead of
    #: leaving the operator with a bot that never answers again.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    #: Wedding photographs are personal data. An abandoned job is swept, and
    #: the operator can bring the moment forward with /forget.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
