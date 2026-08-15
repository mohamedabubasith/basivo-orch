"""The scheduler: when a flow fires next, and firing it.

`next_fire_at` is pure, so the parts that are genuinely hard — timezones,
DST, a cron that has no next match — are tested against fixed instants rather
than by waiting for a clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Flow, FlowSchedule, FlowVersion, Run, RunStatus, TriggerKind
from basivo_orch.flows.scheduler import ScheduleError, next_fire_at, sync_schedule, tick


def scheduled_graph(**config) -> Graph:
    return Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.schedule", "config": config},
                {
                    "id": "set",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "ran", "value": "yes"}]},
                },
            ],
            "edges": [{"source": "t", "target": "set"}],
        }
    )


# ---------------------------------------------------------------------------
# next_fire_at
# ---------------------------------------------------------------------------


def test_interval_adds_its_seconds():
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert next_fire_at(
        mode="interval", cron=None, interval_seconds=300, after=now
    ) == now + timedelta(seconds=300)


def test_cron_finds_the_next_daily_slot():
    now = datetime(2026, 3, 1, 7, 30, tzinfo=UTC)
    assert next_fire_at(mode="cron", cron="0 6 * * *", interval_seconds=None, after=now) == datetime(
        2026, 3, 2, 6, 0, tzinfo=UTC
    )


def test_cron_is_evaluated_in_the_flows_own_timezone():
    """'0 6 * * *' means six in the morning where the user is.

    Asia/Kolkata is UTC+5:30, so their 06:00 is 00:30 UTC — a schedule that
    ignored the timezone would fire five and a half hours late, every day.
    """
    now = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    fires = next_fire_at(
        mode="cron",
        cron="0 6 * * *",
        interval_seconds=None,
        timezone="Asia/Kolkata",
        after=now,
    )
    assert fires == datetime(2026, 3, 1, 0, 30, tzinfo=UTC)
    assert fires.astimezone(ZoneInfo("Asia/Kolkata")).hour == 6


def test_cron_keeps_local_meaning_across_a_dst_change():
    """The clocks move; '0 9 * * *' still means 9am locally.

    New York leaves DST on 2026-11-01. The 9am slot before it is 13:00 UTC
    and the one after is 14:00 UTC — the whole reason the timezone field
    exists rather than storing everything in UTC.
    """
    before = next_fire_at(
        mode="cron",
        cron="0 9 * * *",
        interval_seconds=None,
        timezone="America/New_York",
        after=datetime(2026, 10, 30, 20, 0, tzinfo=UTC),
    )
    after = next_fire_at(
        mode="cron",
        cron="0 9 * * *",
        interval_seconds=None,
        timezone="America/New_York",
        after=datetime(2026, 11, 2, 20, 0, tzinfo=UTC),
    )
    assert before.hour == 13
    assert after.hour == 14
    for moment in (before, after):
        assert moment.astimezone(ZoneInfo("America/New_York")).hour == 9


def test_a_broken_schedule_says_so():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    with pytest.raises(ScheduleError):
        next_fire_at(mode="cron", cron="not a cron", interval_seconds=None, after=now)
    with pytest.raises(ScheduleError):
        next_fire_at(mode="cron", cron=None, interval_seconds=None, after=now)
    with pytest.raises(ScheduleError):
        next_fire_at(mode="interval", cron=None, interval_seconds=None, after=now)
    with pytest.raises(ScheduleError, match="timezone"):
        next_fire_at(
            mode="cron",
            cron="0 6 * * *",
            interval_seconds=None,
            timezone="Mars/Olympus",
            after=now,
        )


# ---------------------------------------------------------------------------
# Publishing arms and disarms the schedule
# ---------------------------------------------------------------------------


async def make_flow(session, organization, graph: Graph) -> Flow:
    import uuid

    flow = Flow(
        organization_id=organization.id, name="Scheduled", slug=f"s-{uuid.uuid4().hex[:8]}"
    )
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
    session.add(version)
    await session.flush()
    flow.published_version_id = version.id
    await session.commit()
    return flow


async def test_publishing_a_scheduled_flow_writes_its_next_run(session, organization):
    graph = scheduled_graph(mode="interval", interval_seconds=3600)
    flow = await make_flow(session, organization, graph)
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    row = await sync_schedule(session, flow=flow, graph=graph, now=now)

    assert row is not None
    assert row.mode == "interval"
    assert row.next_run_at.replace(tzinfo=UTC) == now + timedelta(hours=1)


async def test_republishing_without_the_trigger_stops_the_schedule(session, organization):
    graph = scheduled_graph(mode="interval", interval_seconds=3600)
    flow = await make_flow(session, organization, graph)
    await sync_schedule(session, flow=flow, graph=graph)
    assert await session.get(FlowSchedule, flow.id) is not None

    plain = Graph.model_validate(
        {"nodes": [{"id": "t", "type": "trigger.manual", "config": {}}], "edges": []}
    )
    assert await sync_schedule(session, flow=flow, graph=plain) is None

    session.expunge_all()
    assert await session.get(FlowSchedule, flow.id) is None, "the schedule kept firing"


# ---------------------------------------------------------------------------
# Ticking
# ---------------------------------------------------------------------------


async def test_a_due_flow_is_fired_and_rescheduled(session, organization):
    graph = scheduled_graph(mode="interval", interval_seconds=60)
    flow = await make_flow(session, organization, graph)
    row = await sync_schedule(session, flow=flow, graph=graph)
    assert row is not None

    # Make it due.
    row.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
    await session.commit()

    fired = await tick(session)

    assert fired == [flow.id]
    runs = (await session.execute(select(Run).where(Run.flow_id == flow.id))).scalars().all()
    assert len(runs) == 1
    # Queued, not executed inline: the worker owns execution.
    assert runs[0].status is RunStatus.QUEUED
    assert runs[0].trigger is TriggerKind.SCHEDULE
    assert runs[0].input["payload"]["scheduled"] is True

    await session.refresh(row)
    assert row.next_run_at.replace(tzinfo=UTC) > datetime.now(UTC)
    assert row.last_run_at is not None


async def test_a_flow_that_is_not_due_stays_put(session, organization):
    graph = scheduled_graph(mode="interval", interval_seconds=3600)
    flow = await make_flow(session, organization, graph)
    await sync_schedule(session, flow=flow, graph=graph)

    assert await tick(session) == []
    assert (await session.execute(select(Run))).scalars().first() is None


async def test_a_missed_window_fires_once_not_once_per_missed_slot(session, organization):
    """The server was down for a day. A daily flow owes one run, not 24.

    The next slot is computed forward from now rather than replayed from the
    last one, so a restart cannot produce a stampede of identical runs.
    """
    graph = scheduled_graph(mode="interval", interval_seconds=3600)
    flow = await make_flow(session, organization, graph)
    row = await sync_schedule(session, flow=flow, graph=graph)
    assert row is not None
    row.next_run_at = datetime.now(UTC) - timedelta(days=1)
    await session.commit()

    assert await tick(session) == [flow.id]
    # Immediately ticking again produces nothing: the row moved forward past
    # now, not to the next of twenty-four missed hourly slots in the past.
    assert await tick(session) == []

    runs = (await session.execute(select(Run).where(Run.flow_id == flow.id))).scalars().all()
    assert len(runs) == 1


async def test_an_unpublished_flow_loses_its_schedule_instead_of_retrying_forever(
    session, organization
):
    graph = scheduled_graph(mode="interval", interval_seconds=60)
    flow = await make_flow(session, organization, graph)
    row = await sync_schedule(session, flow=flow, graph=graph)
    assert row is not None
    row.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
    flow.published_version_id = None
    await session.commit()

    assert await tick(session) == []
    session.expunge_all()
    assert await session.get(FlowSchedule, flow.id) is None
