"""What a plan allows, and what happens at each edge of it.

The properties that matter here are the ones a customer notices: a limit that
applies a month late, a grace period that never ends, or a workspace whose runs
are counted against somebody else's plan.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Organization
from basivo_orch.billing import service
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.billing.plans import PLANS
from basivo_orch.billing.service import QuotaExceeded
from basivo_orch.config import Settings, get_settings
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Artifact, Flow, FlowVersion, Run, RunStatus, TriggerKind

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

GRAPH = Graph.model_validate(
    {
        "nodes": [
            {"id": "start", "type": "trigger.manual", "name": "Run manually", "config": {}},
        ],
        "edges": [],
    }
)


async def subscribe(
    session: AsyncSession,
    organization: Organization,
    *,
    plan: str = "pro",
    status: str = SubscriptionStatus.ACTIVE,
    grace_until: datetime | None = None,
    current_period_end: datetime | None = None,
) -> Subscription:
    row = Subscription(
        organization_id=organization.id,
        plan=plan,
        status=status,
        grace_until=grace_until,
        current_period_end=current_period_end,
    )
    session.add(row)
    await session.commit()
    return row


async def add_runs(
    session: AsyncSession,
    organization: Organization,
    count: int,
    *,
    created_at: datetime | None = None,
) -> Flow:
    created_at = created_at or datetime.now(UTC)
    flow = Flow(organization_id=organization.id, name="F", slug=f"f-{uuid.uuid4().hex[:6]}")
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=GRAPH.model_dump(mode="json"))
    session.add(version)
    await session.flush()
    for _ in range(count):
        session.add(
            Run(
                flow_id=flow.id,
                flow_version_id=version.id,
                organization_id=organization.id,
                status=RunStatus.SUCCEEDED,
                trigger=TriggerKind.MANUAL,
                input={},
                created_at=created_at,
            )
        )
    await session.commit()
    return flow


# --- the switch ------------------------------------------------------------


async def test_demo_mode_enforces_nothing(
    session: AsyncSession, organization: Organization, demo_mode: None
):
    """The whole point of the switch: a demo deployment has no limits at all."""
    await add_runs(session, organization, 200)
    await service.check_run_quota(session, organization.id)
    await service.check_flow_quota(session, organization.id)
    await service.check_seat_quota(session, organization.id)
    plan = await service.current_plan(session, organization.id)
    assert plan.runs_per_month is None
    assert await service.history_cutoff(session, organization.id) is None


async def test_demo_mode_ignores_a_subscription_row(
    session: AsyncSession, organization: Organization, demo_mode: None
):
    """A row left over from a production restore must not start billing."""
    await subscribe(session, organization, plan="free", status=SubscriptionStatus.EXPIRED)
    state = await service.entitlement(session, organization.id)
    assert state.mode == "demo"
    assert state.plan.runs_per_month is None


def test_production_mode_needs_its_keys(monkeypatch: pytest.MonkeyPatch):
    """A deployment that says it takes money and cannot must not start."""
    for name in ("DODO_API_KEY", "DODO_WEBHOOK_SECRET", "DODO_PRODUCT_PRO", "DODO_PRODUCT_TEAM"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="DODO_API_KEY"):
        Settings(BILLING_MODE="production", _env_file=None)


def test_demo_mode_needs_nothing():
    assert Settings(BILLING_MODE="demo", _env_file=None).billing_is_live is False


# --- which plan applies ----------------------------------------------------


def test_no_subscription_is_the_free_plan():
    assert service.effective_plan(None, NOW).code == "free"


def test_an_active_subscription_is_its_plan():
    row = Subscription(plan="pro", status=SubscriptionStatus.ACTIVE)
    assert service.effective_plan(row, NOW).code == "pro"


def test_a_failed_payment_keeps_the_plan_during_the_grace_period():
    row = Subscription(
        plan="team",
        status=SubscriptionStatus.PAST_DUE,
        grace_until=NOW + timedelta(days=3),
    )
    assert service.effective_plan(row, NOW).code == "team"


def test_the_plan_falls_back_once_the_grace_period_is_over():
    row = Subscription(
        plan="team",
        status=SubscriptionStatus.PAST_DUE,
        grace_until=NOW - timedelta(seconds=1),
    )
    assert service.effective_plan(row, NOW).code == "free"


def test_a_cancelled_subscription_runs_to_the_end_of_the_paid_period():
    row = Subscription(
        plan="pro",
        status=SubscriptionStatus.CANCELLED,
        current_period_end=NOW + timedelta(days=2),
    )
    assert service.effective_plan(row, NOW).code == "pro"


def test_a_cancelled_subscription_ends_when_the_period_does():
    row = Subscription(
        plan="pro",
        status=SubscriptionStatus.CANCELLED,
        current_period_end=NOW - timedelta(seconds=1),
    )
    assert service.effective_plan(row, NOW).code == "free"


def test_an_expired_subscription_is_the_free_plan():
    row = Subscription(plan="pro", status=SubscriptionStatus.EXPIRED)
    assert service.effective_plan(row, NOW).code == "free"


def test_a_plan_code_we_no_longer_sell_falls_back_instead_of_failing():
    """A retired tier must not lock a workspace out of its own data."""
    row = Subscription(plan="platinum", status=SubscriptionStatus.ACTIVE)
    assert service.effective_plan(row, NOW).code == "free"


def test_naive_timestamps_do_not_break_the_comparison():
    """SQLite hands back naive datetimes; a TypeError here would be an outage."""
    row = Subscription(
        plan="pro",
        status=SubscriptionStatus.PAST_DUE,
        grace_until=(NOW + timedelta(days=1)).replace(tzinfo=None),
    )
    assert service.effective_plan(row, NOW).code == "pro"


# --- runs ------------------------------------------------------------------


async def test_one_run_below_the_limit_is_allowed(
    session: AsyncSession, organization: Organization
):
    await add_runs(session, organization, PLANS["free"].runs_per_month - 1)
    await service.check_run_quota(session, organization.id)


async def test_the_run_at_the_limit_is_refused(session: AsyncSession, organization: Organization):
    await add_runs(session, organization, PLANS["free"].runs_per_month)
    with pytest.raises(QuotaExceeded) as raised:
        await service.check_run_quota(session, organization.id)
    assert raised.value.limit_name == "runs"
    assert "Free" in raised.value.message
    assert "Upgrade" in raised.value.message


async def test_last_months_runs_do_not_count(session: AsyncSession, organization: Organization):
    """The month rolls over and the allowance comes back. Without this, a
    workspace that hit the limit in May would still be blocked in June."""
    await add_runs(session, organization, 500, created_at=datetime.now(UTC) - timedelta(days=40))
    await service.check_run_quota(session, organization.id)
    assert await service.runs_this_month(session, organization.id) == 0


async def test_another_workspaces_runs_do_not_count(
    session: AsyncSession, organization: Organization, other_organization: Organization
):
    await add_runs(session, other_organization, 200)
    await service.check_run_quota(session, organization.id)


async def test_a_paid_plan_gets_its_larger_allowance(
    session: AsyncSession, organization: Organization
):
    await subscribe(session, organization, plan="pro")
    await add_runs(session, organization, PLANS["free"].runs_per_month + 10)
    await service.check_run_quota(session, organization.id)


async def test_creating_a_run_over_the_limit_raises(
    session: AsyncSession, organization: Organization
):
    """The check lives in `create_run`, so every route and the ticker share it."""
    from basivo_orch.flows import service as flows

    flow = await add_runs(session, organization, PLANS["free"].runs_per_month)
    version = (await flows.latest_version(session, flow.id)) if flow else None
    with pytest.raises(QuotaExceeded):
        await flows.create_run(
            session, flow=flow, version=version, trigger=TriggerKind.MANUAL, payload={}
        )


async def test_a_retried_delivery_still_gets_its_run_back(
    session: AsyncSession, organization: Organization
):
    """Idempotency is checked before the quota is.

    A provider that retries a webhook after a timeout must be told about the
    run it already started, not refused for a run that was already counted.
    """
    from basivo_orch.flows import service as flows

    flow = await add_runs(session, organization, 0)
    version = await flows.latest_version(session, flow.id)
    first, created = await flows.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.WEBHOOK,
        payload={},
        idempotency_key="delivery-1",
    )
    assert created
    await add_runs(session, organization, PLANS["free"].runs_per_month)

    again, created_again = await flows.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.WEBHOOK,
        payload={},
        idempotency_key="delivery-1",
    )
    assert again.id == first.id
    assert created_again is False


# --- flows, seats, history -------------------------------------------------


async def test_the_flow_after_the_last_free_one_is_refused(
    session: AsyncSession, organization: Organization
):
    from basivo_orch.flows import service as flows

    for index in range(PLANS["free"].flows):
        await flows.create_flow(
            session,
            organization_id=organization.id,
            user_id=None,
            name=f"Flow {index}",
            slug=None,
            description=None,
            graph=GRAPH,
        )
    with pytest.raises(QuotaExceeded) as raised:
        await flows.create_flow(
            session,
            organization_id=organization.id,
            user_id=None,
            name="One too many",
            slug=None,
            description=None,
            graph=GRAPH,
        )
    assert raised.value.limit_name == "flows"


async def test_a_paid_plan_has_unlimited_flows(session: AsyncSession, organization: Organization):
    from basivo_orch.flows import service as flows

    await subscribe(session, organization, plan="pro")
    for index in range(PLANS["free"].flows + 2):
        await flows.create_flow(
            session,
            organization_id=organization.id,
            user_id=None,
            name=f"Flow {index}",
            slug=None,
            description=None,
            graph=GRAPH,
        )


# --- apps and storage ------------------------------------------------------


async def test_the_app_after_the_last_free_one_is_refused(
    session: AsyncSession, organization: Organization
):
    """Apps are capped apart from flows: each message runs a coding agent."""
    from basivo_orch.appbuilder import service as apps

    for index in range(PLANS["free"].apps):
        await apps.create_project(session, organization_id=organization.id, name=f"App {index}")
    with pytest.raises(QuotaExceeded) as raised:
        await apps.create_project(session, organization_id=organization.id, name="One too many")
    assert raised.value.limit_name == "apps"

    # And nothing half made: the refused app left no flow behind.
    flows = await session.execute(select(Flow).where(Flow.organization_id == organization.id))
    assert len(list(flows.scalars().all())) == PLANS["free"].apps


async def test_storage_counts_files_and_uploads_together(
    session: AsyncSession, organization: Organization
):
    """The limit is disk, so everything on it counts: rendered files, app
    builds, and the images somebody uploaded to an app."""
    from basivo_orch.appbuilder import service as apps
    from basivo_orch.flows.models import Artifact

    session.add(
        Artifact(
            organization_id=organization.id,
            filename="poster.png",
            size_bytes=300_000,
            data=b"x" * 10,
        )
    )
    await session.commit()

    project = await apps.create_project(session, organization_id=organization.id, name="Shop")
    await apps.add_asset(
        session,
        project=project,
        filename="logo.png",
        data=b"\x89PNG\r\n\x1a\n" + b"y" * 200_000,
    )
    assert await service.storage_bytes(session, organization.id) == 300_000 + 200_008


async def test_the_file_that_would_go_over_the_plan_is_refused(
    session: AsyncSession, organization: Organization, monkeypatch: pytest.MonkeyPatch
):
    """Checked before the write, because disk is shared with every other
    workspace on the deployment."""
    from dataclasses import replace

    from basivo_orch.billing import pricing

    monkeypatch.setitem(PLANS, "free", replace(PLANS["free"], storage_mb=1))
    pricing.invalidate()

    session.add(
        Artifact(
            organization_id=organization.id,
            filename="render.mp4",
            size_bytes=900_000,
            data=b"x" * 10,
        )
    )
    await session.commit()

    await service.check_storage_quota(session, organization.id, adding=100_000)
    with pytest.raises(QuotaExceeded) as raised:
        await service.check_storage_quota(session, organization.id, adding=200_000)
    assert raised.value.limit_name == "storage"
    assert "MB" in raised.value.message


async def test_the_second_seat_on_the_free_plan_is_refused(
    session: AsyncSession, organization: Organization
):
    from basivo_orch.auth.models import Membership, User

    user = User(email="a@example.com", hashed_password="x", is_active=True)  # noqa: S106
    session.add(user)
    await session.flush()
    session.add(Membership(user_id=user.id, organization_id=organization.id, role="owner"))
    await session.commit()

    with pytest.raises(QuotaExceeded) as raised:
        await service.check_seat_quota(session, organization.id)
    assert raised.value.limit_name == "seats"
    assert "1 seat" in raised.value.message


async def test_history_is_windowed_but_nothing_is_deleted(
    session: AsyncSession, organization: Organization
):
    from basivo_orch.flows import service as flows

    old = datetime.now(UTC) - timedelta(days=30)
    await add_runs(session, organization, 1, created_at=old)
    await add_runs(session, organization, 1)

    listed = await flows.list_runs(session, organization_id=organization.id)
    assert len(listed) == 1
    # Still counted, and still there: the row was hidden, not removed.
    assert await service.runs_this_month(session, organization.id) == 1
    stored = (await session.execute(select(Run))).scalars().all()
    assert len(stored) == 2


async def test_a_paid_plan_sees_further_back(session: AsyncSession, organization: Organization):
    from basivo_orch.flows import service as flows

    await subscribe(session, organization, plan="pro")
    await add_runs(session, organization, 1, created_at=datetime.now(UTC) - timedelta(days=20))
    assert len(await flows.list_runs(session, organization_id=organization.id)) == 1


async def test_a_schedule_over_quota_skips_instead_of_dying(
    session: AsyncSession, organization: Organization
):
    """A plan limit must not stop the ticker or delete the schedule."""
    from basivo_orch.flows import scheduler
    from basivo_orch.flows.models import FlowSchedule

    flow = await add_runs(session, organization, PLANS["free"].runs_per_month)
    version = FlowVersion(flow_id=flow.id, version=2, graph=GRAPH.model_dump(mode="json"))
    session.add(version)
    await session.flush()
    flow.published_version_id = version.id
    row = FlowSchedule(
        flow_id=flow.id,
        organization_id=organization.id,
        mode="interval",
        interval_seconds=60,
        next_run_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()

    fired = await scheduler._fire(session, row, None)
    assert fired is False
    assert await session.get(FlowSchedule, row.flow_id) is not None
