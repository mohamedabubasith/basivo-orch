"""Flow and run orchestration: the layer between HTTP and the engine."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from basivo_orch.db import SessionLocal
from basivo_orch.flows import nodes as node_registry
from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.graph import Graph, validate_graph
from basivo_orch.flows.models import Flow, FlowVersion, Run, RunStatus, TriggerKind
from basivo_orch.flows.schemas import slugify
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: Strong references to in-flight background runs.
#:
#: asyncio only holds a weak reference to a task, so a task nobody keeps can be
#: garbage-collected mid-execution — the run would simply stop, with the row
#: left RUNNING forever and nothing in the log to say why.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def validate(graph: Graph) -> None:
    """Raise `GraphError` if the graph will not execute."""
    validate_graph(graph, known_types=node_registry.REGISTRY)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


async def create_flow(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    name: str,
    slug: str | None,
    description: str | None,
    graph: Graph,
) -> tuple[Flow, FlowVersion]:
    candidate = slug or slugify(name)

    flow = Flow(
        organization_id=organization_id,
        name=name,
        slug=candidate,
        description=description,
        created_by=user_id,
    )
    session.add(flow)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ValueError(f"A flow with the slug {candidate!r} already exists.") from None

    version = FlowVersion(
        flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"), created_by=user_id
    )
    session.add(version)
    await session.commit()
    await session.refresh(flow)
    return flow, version


async def latest_version(session: AsyncSession, flow_id: uuid.UUID) -> FlowVersion:
    result = await session.execute(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise ValueError("This flow has no versions.")
    return version


async def save_version(
    session: AsyncSession,
    *,
    flow: Flow,
    graph: Graph,
    user_id: uuid.UUID | None,
) -> FlowVersion:
    """Append a new version. Existing versions are never modified.

    Every edit is a new row rather than an update, so a run from last week
    still points at the graph that actually executed. Without that, reading an
    old run's log alongside today's flow is actively misleading.
    """
    current = await latest_version(session, flow.id)
    version = FlowVersion(
        flow_id=flow.id,
        version=current.version + 1,
        graph=graph.model_dump(mode="json"),
        created_by=user_id,
    )
    session.add(version)
    flow.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(version)
    return version


async def publish(session: AsyncSession, *, flow: Flow, user_id: uuid.UUID | None) -> FlowVersion:
    """Make the latest version the one external callers execute.

    Validation happens here as well as on save. Save-time validation can be
    bypassed by a graph written before a node type changed, and publishing is
    the moment a flow becomes reachable from outside — the last point at which
    rejecting it costs nobody anything.
    """
    version = await latest_version(session, flow.id)
    graph = Graph.model_validate(version.graph)
    validate(graph)

    version.published_at = datetime.now(UTC)
    flow.published_version_id = version.id
    await session.commit()
    await session.refresh(version)
    log.info("flow.published", flow_id=str(flow.id), version=version.version)
    return version


async def get_flow(
    session: AsyncSession, *, organization_id: uuid.UUID, flow_id: uuid.UUID
) -> Flow | None:
    """Fetch a flow *within a tenant*.

    organization_id is part of the query, not checked afterwards: a filter that
    is applied at the database is one that cannot be forgotten by a later
    caller of this function.
    """
    result = await session.execute(
        select(Flow).where(Flow.id == flow_id, Flow.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def list_flows(
    session: AsyncSession, *, organization_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[Flow]:
    result = await session.execute(
        select(Flow)
        .where(Flow.organization_id == organization_id)
        .order_by(Flow.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def create_run(
    session: AsyncSession,
    *,
    flow: Flow,
    version: FlowVersion,
    trigger: TriggerKind,
    payload: dict[str, Any],
    user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> tuple[Run, bool]:
    """Create a queued run. Returns (run, created).

    When `idempotency_key` matches an existing run for this flow, that run is
    returned and nothing new is started. Webhook providers retry aggressively
    on timeout, and a flow that charges a card must not run twice because the
    first response was slow.
    """
    if idempotency_key:
        existing = await session.execute(
            select(Run).where(Run.flow_id == flow.id, Run.idempotency_key == idempotency_key)
        )
        if found := existing.scalar_one_or_none():
            return found, False

    run = Run(
        flow_id=flow.id,
        flow_version_id=version.id,
        organization_id=flow.organization_id,
        trigger=trigger,
        input={"payload": payload, "fired_at": datetime.now(UTC).isoformat()},
        started_by=user_id,
        idempotency_key=idempotency_key,
        status=RunStatus.QUEUED,
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        # Two identical requests raced. The other one won; return its run.
        await session.rollback()
        existing = await session.execute(
            select(Run).where(Run.flow_id == flow.id, Run.idempotency_key == idempotency_key)
        )
        if found := existing.scalar_one_or_none():
            return found, False
        raise

    await session.refresh(run)
    return run, True


async def execute(
    session: AsyncSession, *, run: Run, graph: Graph, redis_client: RedisClient | None
) -> Run:
    """Run a flow to completion using the caller's session."""
    engine = Engine(session, run=run, graph=graph, redis_client=redis_client)
    return await engine.execute()


def execute_detached(run_id: uuid.UUID, graph: Graph, redis_client: RedisClient | None) -> None:
    """Start a run in the background and return immediately.

    Uses its own session: the request's session is closed as soon as the 202
    response is sent, and continuing to use it would fail on the first query
    after that.

    This is in-process on purpose for now. It is the right shape for a beta and
    the wrong shape for scale — a restart loses in-flight runs, and nothing
    balances load across workers. The fix is a real queue, and the seam is
    here: this function is the only thing that would change.
    """

    async def runner() -> None:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            if run is None:
                log.error("run.vanished", run_id=str(run_id))
                return
            try:
                await Engine(session, run=run, graph=graph, redis_client=redis_client).execute()
            except Exception as exc:
                log.exception("run.crashed", run_id=str(run_id), error=str(exc))

    task = asyncio.create_task(runner())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def get_run(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    run_id: uuid.UUID,
    with_nodes: bool = False,
) -> Run | None:
    statement = select(Run).where(Run.id == run_id, Run.organization_id == organization_id)
    if with_nodes:
        statement = statement.options(selectinload(Run.node_executions))
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    flow_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Run]:
    statement = select(Run).where(Run.organization_id == organization_id)
    if flow_id is not None:
        statement = statement.where(Run.flow_id == flow_id)
    if status is not None:
        statement = statement.where(Run.status == status)
    result = await session.execute(
        statement.order_by(Run.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars())


async def run_stats(
    session: AsyncSession, *, organization_id: uuid.UUID, flow_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Roll-up feeding the analysis layer (SOW section 3).

    Deliberately computed in SQL rather than by loading runs into Python: the
    point of giving `node_execution` real columns was that these questions stay
    answerable when there are millions of rows.
    """
    statement = select(Run.status, func.count(), func.avg(Run.duration_ms)).where(
        Run.organization_id == organization_id
    )
    if flow_id is not None:
        statement = statement.where(Run.flow_id == flow_id)
    result = await session.execute(statement.group_by(Run.status))

    by_status: dict[str, dict[str, Any]] = {}
    total = 0
    for status_value, count, avg_ms in result:
        by_status[str(status_value)] = {
            "count": count,
            "avg_duration_ms": int(avg_ms) if avg_ms is not None else None,
        }
        total += count

    succeeded = by_status.get(RunStatus.SUCCEEDED.value, {}).get("count", 0)
    return {
        "total": total,
        "by_status": by_status,
        "success_rate": round(succeeded / total, 4) if total else None,
    }
