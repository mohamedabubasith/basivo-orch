"""Flow and run orchestration: the layer between HTTP and the engine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from basivo_orch.billing import service as billing
from basivo_orch.flows import nodes as node_registry
from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.graph import Graph, validate_graph
from basivo_orch.flows.models import (
    Flow,
    FlowSchedule,
    FlowVersion,
    Run,
    RunStatus,
    TriggerKind,
)
from basivo_orch.flows.schemas import slugify
from basivo_orch.logging import get_logger

log = get_logger(__name__)


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
    # A slug the caller *asked* for is theirs: a collision is a real conflict and
    # they need to hear about it. One derived from the name is our own detail, so
    # a second flow called "Untitled flow" becomes `untitled-flow-2` rather than
    # an error. Not hypothetical: with creation reduced to one click, every
    # user's second flow collided.
    # Plan limits are checked here rather than in the route, because the
    # template installer creates flows too and would otherwise be a way past
    # the limit. `check_flow_quota` is a no-op while billing is switched off.
    await billing.check_flow_quota(session, organization_id)

    explicit = bool(slug)
    base = slug or slugify(name)
    candidate = base

    if not explicit:
        # Chosen by looking first, rather than by inserting and recovering. The
        # recovery is a `session.rollback()`, and that expires every object in
        # the session — including flows the caller is still holding, whose next
        # attribute read then becomes a lazy load from sync code and a
        # MissingGreenlet. Asking costs one SELECT.
        taken = set(
            (
                await session.execute(
                    select(Flow.slug).where(
                        Flow.organization_id == organization_id,
                        Flow.slug.like(f"{base}%"),
                    )
                )
            ).scalars()
        )
        if base in taken:
            candidate = next(
                slug for slug in (f"{base}-{n}" for n in range(2, 1000)) if slug not in taken
            )

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
        # Two requests picked the same free slug at the same moment. The unique
        # constraint is what makes that safe rather than silent.
        await session.rollback()
        raise ValueError(
            f"A flow with the slug {candidate!r} already exists."
            if explicit
            else "Two flows were created at the same moment. Try again."
        ) from None

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


async def versions(
    session: AsyncSession, flow_id: uuid.UUID, *, limit: int = 50
) -> list[FlowVersion]:
    """The edit history, newest first.

    Versions are append-only, so this is the whole story of a flow: what it
    looked like at every save, and which of those is the one production runs.
    """
    result = await session.execute(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def restore_version(
    session: AsyncSession, *, flow: Flow, version_number: int, user_id: uuid.UUID | None
) -> FlowVersion:
    """Bring an old graph back as a NEW version.

    History is never rewritten, and the old version keeps its number: a run
    from three weeks ago has to keep describing the graph that actually ran.
    Restoring is therefore an ordinary save whose contents came from earlier,
    which also means an undo of a restore is another restore.
    """
    result = await session.execute(
        select(FlowVersion).where(
            FlowVersion.flow_id == flow.id, FlowVersion.version == version_number
        )
    )
    wanted = result.scalar_one_or_none()
    if wanted is None:
        raise ValueError(f"This flow has no version {version_number}.")
    return await save_version(
        session, flow=flow, graph=Graph.model_validate(wanted.graph), user_id=user_id
    )


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

    # Publishing is what arms a schedule — and what disarms one, when the
    # trigger has been removed from the graph being published.
    from basivo_orch.flows.scheduler import sync_schedule

    await sync_schedule(session, flow=flow, graph=graph)

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


async def summarise_flows(
    session: AsyncSession, flows: list[Flow]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Per-flow facts the list needs: size, trigger, and the last run.

    Three queries for the whole page rather than three per row. A list that
    fires a request per flow is how a page with twenty flows becomes a page
    that takes two seconds and hammers the database.
    """
    if not flows:
        return {}

    ids = [flow.id for flow in flows]
    summary: dict[uuid.UUID, dict[str, Any]] = {
        flow.id: {
            "node_count": 0,
            "trigger_type": None,
            "last_run_status": None,
            "last_run_at": None,
            "next_run_at": None,
        }
        for flow in flows
    }

    # The version each flow actually presents: what is published, or the
    # latest draft when nothing is. Reading the graph is how node count and
    # trigger type are known at all — they are not columns.
    latest = (
        select(FlowVersion.flow_id, func.max(FlowVersion.version).label("version"))
        .where(FlowVersion.flow_id.in_(ids))
        .group_by(FlowVersion.flow_id)
        .subquery()
    )
    versions = await session.execute(
        select(FlowVersion).join(
            latest,
            (FlowVersion.flow_id == latest.c.flow_id) & (FlowVersion.version == latest.c.version),
        )
    )
    published_ids = {flow.published_version_id for flow in flows if flow.published_version_id}
    published = await session.execute(
        select(FlowVersion).where(FlowVersion.id.in_(published_ids or {uuid.uuid4()}))
    )
    by_flow: dict[uuid.UUID, FlowVersion] = {v.flow_id: v for v in versions.scalars()}
    by_flow.update({v.flow_id: v for v in published.scalars()})

    for flow_id, version in by_flow.items():
        graph = version.graph or {}
        nodes = graph.get("nodes") or []
        summary[flow_id]["node_count"] = len(nodes)
        trigger = next(
            (n.get("type") for n in nodes if str(n.get("type", "")).startswith("trigger.")), None
        )
        summary[flow_id]["trigger_type"] = trigger

    # The most recent run per flow, in one pass.
    runs = await session.execute(
        select(Run.flow_id, Run.status, Run.created_at)
        .where(Run.flow_id.in_(ids))
        .order_by(Run.flow_id, Run.created_at.desc())
    )
    for flow_id, status, created_at in runs:
        entry = summary[flow_id]
        if entry["last_run_at"] is None:
            entry["last_run_status"] = str(status)
            entry["last_run_at"] = created_at

    schedules = await session.execute(
        select(FlowSchedule.flow_id, FlowSchedule.next_run_at).where(FlowSchedule.flow_id.in_(ids))
    )
    for flow_id, next_run_at in schedules:
        summary[flow_id]["next_run_at"] = next_run_at

    return summary


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

    # After the idempotency lookup on purpose: a provider retrying a delivery
    # must get back the run it already started, not a refusal for a run that
    # was already counted against the plan.
    await billing.check_run_quota(session, flow.organization_id)

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


def enqueue(run: Run) -> None:
    """Hand a run to the workers. Returns immediately.

    There is nothing to send: `create_run` already wrote the row as QUEUED,
    and QUEUED *is* the queue — `basivo_orch.worker` claims from it with
    `FOR UPDATE SKIP LOCKED`. This function exists to name the moment, and
    because the alternative reads like a bug: a route that creates a run and
    then does nothing looks like a forgotten line.

    This replaced an `asyncio.create_task` inside the API process, which meant
    every deploy, reload or OOM silently killed the runs in flight. A queued
    row survives all three; whichever worker is alive picks it up.
    """
    log.info("run.enqueued", run_id=str(run.id), flow_id=str(run.flow_id))


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
    # How far back a plan lists runs. Older runs are still stored and still
    # counted; the smaller plans simply do not show them, which is what the
    # bigger plans sell. Nothing is deleted for being on a smaller plan.
    cutoff = await billing.history_cutoff(session, organization_id)
    if cutoff is not None:
        statement = statement.where(Run.created_at >= cutoff)
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
