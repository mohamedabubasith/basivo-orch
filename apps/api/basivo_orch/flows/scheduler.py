"""The thing that makes Scheduler Trigger a trigger.

Until this existed the node was a promise the product did not keep: a user
could drop it on a canvas, set '0 6 * * *', publish, and nothing would ever
happen. Three pieces here:

* `next_fire_at` — when a schedule fires next. Pure, so the awkward parts
  (timezones, DST, a cron that never matches) are testable without a clock.
* `sync_schedule` — publish writes the schedule row, or deletes it. The
  *published* graph decides, not the draft, so an editor mid-experiment never
  fires anything.
* `tick` / `run_scheduler` — claim what is due and start it.

Missed windows do not stampede. If the process was down for a day, a daily
flow fires once and moves on, because the next slot is computed forward from
now rather than replayed from the last one. A backlog of 24 identical runs at
boot is not catch-up, it is an incident.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.db import SessionLocal
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Flow, FlowSchedule, FlowVersion, TriggerKind
from basivo_orch.flows.nodes.triggers import ScheduleTriggerConfig
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: How often the ticker looks for due flows. The floor on `interval_seconds`
#: is 30s, so this keeps the worst-case lateness well under one slot.
TICK_SECONDS = 10

#: Most flows a single tick will start. A ticker that tries to launch a
#: thousand runs at once is a thundering herd; the rest are simply still due
#: on the next tick, ten seconds later.
MAX_PER_TICK = 50


class ScheduleError(ValueError):
    """A schedule that cannot produce a next fire time."""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"Unknown timezone {name!r}.") from exc


def next_fire_at(
    *,
    mode: str,
    cron: str | None,
    interval_seconds: int | None,
    timezone: str = "UTC",
    after: datetime,
) -> datetime:
    """The first fire time strictly after `after`, in UTC.

    Cron is evaluated in the flow's own timezone — '0 6 * * *' means six in
    the morning where the user lives, and keeps meaning that across a DST
    change, which is the entire reason the field exists.
    """
    if after.tzinfo is None:
        raise ScheduleError("`after` must be timezone-aware.")

    if mode == "cron":
        if not cron:
            raise ScheduleError("Cron mode needs a cron expression.")
        zone = _zone(timezone)
        try:
            cursor = croniter(cron, after.astimezone(zone))
            local_next = cursor.get_next(datetime)
        except (CroniterBadCronError, ValueError) as exc:
            raise ScheduleError(f"{cron!r} is not a valid cron expression.") from exc
        return local_next.astimezone(UTC)

    if not interval_seconds:
        raise ScheduleError("Interval mode needs interval_seconds.")
    return after.astimezone(UTC) + timedelta(seconds=interval_seconds)


def schedule_config_of(graph: Graph) -> ScheduleTriggerConfig | None:
    """The graph's schedule trigger config, if it has one."""
    node = next((node for node in graph.nodes if node.type == "trigger.schedule"), None)
    if node is None:
        return None
    return ScheduleTriggerConfig.model_validate(node.config)


async def sync_schedule(
    session: AsyncSession, *, flow: Flow, graph: Graph, now: datetime | None = None
) -> FlowSchedule | None:
    """Make the schedule row match the graph being published. Returns it.

    Called on publish. A graph with no schedule trigger deletes the row, so
    "remove the trigger, republish" actually stops the schedule instead of
    leaving an orphan firing forever.
    """
    now = now or datetime.now(UTC)
    config = schedule_config_of(graph)
    existing = await session.get(FlowSchedule, flow.id)

    if config is None:
        if existing is not None:
            await session.delete(existing)
            await session.commit()
            log.info("schedule.removed", flow_id=str(flow.id))
        return None

    upcoming = next_fire_at(
        mode=config.mode,
        cron=config.cron,
        interval_seconds=config.interval_seconds,
        timezone=config.timezone,
        after=now,
    )

    row = existing or FlowSchedule(flow_id=flow.id, organization_id=flow.organization_id)
    row.organization_id = flow.organization_id
    row.mode = config.mode
    row.cron = config.cron
    row.interval_seconds = config.interval_seconds
    row.timezone = config.timezone
    # Republishing re-bases the next fire on the new definition; a user who
    # changes '0 6 * * *' to '0 7 * * *' means the change to take effect now,
    # not after one more run at the old time.
    row.next_run_at = upcoming
    session.add(row)
    await session.commit()
    log.info("schedule.set", flow_id=str(flow.id), next_run_at=upcoming.isoformat())
    return row


async def _fire(session: AsyncSession, row: FlowSchedule, redis_client: RedisClient | None) -> bool:
    """Start one due flow. Returns whether a run was actually created."""
    from basivo_orch.flows import service

    flow = await session.get(Flow, row.flow_id)
    if flow is None or flow.published_version_id is None:
        # Unpublished since the row was written. Drop the schedule rather than
        # retry every ten seconds forever.
        await session.delete(row)
        await session.commit()
        log.info("schedule.dropped_unpublished", flow_id=str(row.flow_id))
        return False

    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        await session.delete(row)
        await session.commit()
        return False

    fired_at = datetime.now(UTC)
    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.SCHEDULE,
        payload={"fired_at": fired_at.isoformat(), "scheduled": True},
    )
    if created:
        service.enqueue(run)
        log.info("schedule.fired", flow_id=str(flow.id), run_id=str(run.id))
    return created


async def tick(
    session: AsyncSession, *, redis_client: RedisClient | None = None, now: datetime | None = None
) -> list[uuid.UUID]:
    """Fire everything due. Returns the flow ids started.

    Claim-then-fire: `next_run_at` is pushed forward and committed *before*
    the run starts, so a tick that overlaps a slow one — or a second process —
    cannot fire the same slot twice. Paying for a duplicate agent run because
    two ticks raced is exactly the kind of bug that shows up on a bill.
    """
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(FlowSchedule)
        .where(FlowSchedule.next_run_at <= now)
        .order_by(FlowSchedule.next_run_at)
        .limit(MAX_PER_TICK)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars())
    if not due:
        return []

    fired: list[uuid.UUID] = []
    for row in due:
        try:
            row.next_run_at = next_fire_at(
                mode=row.mode,
                cron=row.cron,
                interval_seconds=row.interval_seconds,
                timezone=row.timezone,
                after=now,
            )
        except ScheduleError as exc:
            # A schedule that cannot say when it fires next would spin on every
            # tick. Drop it loudly instead.
            log.error("schedule.invalid", flow_id=str(row.flow_id), error=str(exc))
            await session.delete(row)
            await session.commit()
            continue

        row.last_run_at = now
        await session.commit()

        if await _fire(session, row, redis_client):
            fired.append(row.flow_id)

    return fired


async def run_scheduler(redis_client: RedisClient | None) -> None:
    """The ticker loop. Runs for the lifetime of the process.

    In-process, like `execute_detached`, and for the same reason: it is the
    right shape for a beta and the wrong shape for scale. The claim above is
    already multi-worker-safe, so the seam for a real distributed scheduler is
    a deployment change, not a rewrite.
    """
    log.info("scheduler.started", tick_seconds=TICK_SECONDS)
    while True:
        try:
            async with SessionLocal() as session:
                await tick(session, redis_client=redis_client)
        except asyncio.CancelledError:
            log.info("scheduler.stopped")
            raise
        except Exception as exc:  # noqa: BLE001 — one bad tick must not end the loop
            log.exception("scheduler.tick_failed", error=str(exc))
        await asyncio.sleep(TICK_SECONDS)


async def clear_schedule(session: AsyncSession, flow_id: uuid.UUID) -> None:
    """Forget a flow's schedule. Used when a flow is deleted."""
    await session.execute(delete(FlowSchedule).where(FlowSchedule.flow_id == flow_id))
