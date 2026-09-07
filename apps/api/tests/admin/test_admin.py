"""The platform admin API.

Two properties carry the weight here. A customer must not be able to reach any
of it, and an edited price must be the one that is actually enforced — a
pricing screen that changes a number nobody applies is worse than no screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.admin import stats
from basivo_orch.admin.deps import require_platform_admin
from basivo_orch.admin.router import (
    PlanUpdate,
    edit_plan,
    list_plans,
    read_errors,
    read_overview,
    read_workspaces,
    reset_plan,
)
from basivo_orch.auth.models import Organization, User
from basivo_orch.billing import service
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.billing.plans import PLANS
from basivo_orch.billing.service import QuotaExceeded
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import (
    Flow,
    FlowVersion,
    NodeExecution,
    NodeStatus,
    Run,
    RunStatus,
    TriggerKind,
)
from tests.admin.conftest import FakeRequest

GRAPH = Graph.model_validate(
    {
        "nodes": [{"id": "start", "type": "trigger.manual", "name": "Run manually", "config": {}}],
        "edges": [],
    }
)


async def seed_runs(
    session: AsyncSession,
    organization: Organization,
    *,
    succeeded: int = 0,
    failed: int = 0,
    error: str = "boom",
    node_type: str = "agent.llm",
) -> None:
    flow = Flow(organization_id=organization.id, name="F", slug=f"f-{uuid.uuid4().hex[:6]}")
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=GRAPH.model_dump(mode="json"))
    session.add(version)
    await session.flush()

    for index in range(succeeded + failed):
        is_failure = index >= succeeded
        run = Run(
            flow_id=flow.id,
            flow_version_id=version.id,
            organization_id=organization.id,
            status=RunStatus.FAILED if is_failure else RunStatus.SUCCEEDED,
            trigger=TriggerKind.MANUAL,
            input={},
            error=error if is_failure else None,
            created_at=datetime.now(UTC),
            duration_ms=1200,
        )
        session.add(run)
        await session.flush()
        session.add(
            NodeExecution(
                run_id=run.id,
                node_id="n1",
                node_type=node_type,
                status=NodeStatus.FAILED if is_failure else NodeStatus.SUCCEEDED,
                error=f"{error} for run {run.id}" if is_failure else None,
                duration_ms=1200,
                started_at=datetime.now(UTC),
            )
        )
    await session.commit()


# --- who may look ----------------------------------------------------------


async def test_a_customer_is_told_the_admin_api_does_not_exist(customer: User):
    """404, not 403. A 403 confirms that an API listing every workspace is
    there to be attacked."""
    with pytest.raises(HTTPException) as raised:
        await require_platform_admin(FakeRequest(), user=customer)  # type: ignore[arg-type]
    assert raised.value.status_code == 404


async def test_staff_may_look(staff: User):
    assert await require_platform_admin(FakeRequest(), user=staff) is staff  # type: ignore[arg-type]


# --- pricing ---------------------------------------------------------------


async def test_the_plans_start_at_their_built_in_numbers(session: AsyncSession, staff: User):
    plans = await list_plans(staff, session)
    assert [plan.code for plan in plans] == ["free", "pro", "team"]
    assert all(plan.overridden == [] for plan in plans)
    pro = next(plan for plan in plans if plan.code == "pro")
    assert pro.price_inr == PLANS["pro"].price_inr
    assert pro.product_id == "pdt_pro"


async def test_an_edited_price_is_what_the_customer_is_shown(
    session: AsyncSession, staff: User, organization: Organization
):
    await edit_plan(
        "pro",
        PlanUpdate(price_inr="₹1,999", price_usd="$24", product_id="pdt_pro_v2"),
        admin=staff,
        session=session,
    )
    plans = await list_plans(staff, session)
    pro = next(plan for plan in plans if plan.code == "pro")
    assert pro.price_inr == "₹1,999"
    assert pro.product_id == "pdt_pro_v2"
    assert set(pro.overridden) == {"price_inr", "price_usd"}


async def test_an_edited_limit_is_the_one_enforced(
    session: AsyncSession, staff: User, organization: Organization
):
    """The point of the screen: a number changed here decides real refusals."""
    await edit_plan("free", PlanUpdate(runs_per_month=2), admin=staff, session=session)

    await seed_runs(session, organization, succeeded=1)
    await service.check_run_quota(session, organization.id)

    await seed_runs(session, organization, succeeded=1)
    with pytest.raises(QuotaExceeded) as raised:
        await service.check_run_quota(session, organization.id)
    assert "2 runs" in raised.value.message


async def test_unlimited_is_written_as_minus_one(
    session: AsyncSession, staff: User, organization: Organization
):
    await edit_plan("free", PlanUpdate(runs_per_month=-1), admin=staff, session=session)
    await seed_runs(session, organization, succeeded=5)
    plan = await service.current_plan(session, organization.id)
    assert plan.runs_per_month is None
    await service.check_run_quota(session, organization.id)


async def test_a_negative_limit_other_than_minus_one_is_refused():
    with pytest.raises(ValueError, match="whole number"):
        PlanUpdate(seats=-4)


async def test_resetting_puts_the_built_in_numbers_back(
    session: AsyncSession, staff: User, organization: Organization
):
    await edit_plan("free", PlanUpdate(runs_per_month=2), admin=staff, session=session)
    restored = await reset_plan("free", staff, session)
    assert restored.runs_per_month == PLANS["free"].runs_per_month
    assert restored.overridden == []
    plan = await service.current_plan(session, organization.id)
    assert plan.runs_per_month == PLANS["free"].runs_per_month


async def test_clearing_one_field_leaves_the_others_alone(session: AsyncSession, staff: User):
    await edit_plan("pro", PlanUpdate(price_inr="₹1,999", seats=9), admin=staff, session=session)
    after = await edit_plan("pro", PlanUpdate(reset=["price_inr"]), admin=staff, session=session)
    assert after.price_inr == PLANS["pro"].price_inr
    assert after.seats == 9


async def test_a_plan_that_does_not_exist_is_a_404(session: AsyncSession, staff: User):
    with pytest.raises(HTTPException) as raised:
        await edit_plan("platinum", PlanUpdate(seats=1), admin=staff, session=session)
    assert raised.value.status_code == 404


async def test_an_edit_reaches_the_running_process_at_once(
    session: AsyncSession, staff: User, organization: Organization
):
    """The catalogue is cached for a few seconds; an edit must not wait for it."""
    assert (await service.current_plan(session, organization.id)).seats == PLANS["free"].seats
    await edit_plan("free", PlanUpdate(seats=4), admin=staff, session=session)
    assert (await service.current_plan(session, organization.id)).seats == 4


async def test_a_plan_may_list_at_most_twelve_features():
    with pytest.raises(ValueError, match="12 features"):
        PlanUpdate(features=[f"feature {n}" for n in range(13)])


# --- statistics ------------------------------------------------------------


async def test_the_overview_counts_what_happened(
    session: AsyncSession, staff: User, organization: Organization
):
    await seed_runs(session, organization, succeeded=3, failed=1)
    view = await read_overview(7, staff, session)

    assert view["runs"]["total"] == 4
    assert view["runs"]["by_status"]["failed"] == 1
    assert view["runs"]["failure_rate"] == 0.25
    assert view["workspaces"]["total"] == 1
    assert view["workspaces"]["free"] == 1
    assert view["people"]["total"] >= 1
    assert view["slowest_nodes"][0]["node_type"] == "agent.llm"


async def test_the_overview_separates_paying_workspaces(
    session: AsyncSession, staff: User, organization: Organization
):
    session.add(
        Subscription(organization_id=organization.id, plan="pro", status=SubscriptionStatus.ACTIVE)
    )
    await session.commit()
    view = await read_overview(7, staff, session)
    assert view["workspaces"]["paying"] == 1
    assert view["workspaces"]["free"] == 0
    assert view["workspaces"]["by_plan"]["pro:active"] == 1


async def test_errors_are_grouped_so_one_bug_is_one_line(
    session: AsyncSession, staff: User, organization: Organization
):
    """Each failure carries its own run id. Without normalising, twenty copies
    of one bug look like twenty different problems."""
    await seed_runs(session, organization, failed=3, error="Timed out talking to GitHub")
    grouped = await read_errors(7, 20, "grouped", staff, session)

    assert len(grouped["items"]) == 1
    top = grouped["items"][0]
    assert top["count"] == 3
    assert top["node_type"] == "agent.llm"
    assert "<id>" in top["signature"]


async def test_recent_failures_name_the_workspace_but_not_its_data(
    session: AsyncSession, staff: User, organization: Organization
):
    await seed_runs(session, organization, failed=1, error="Timed out")
    recent = await read_errors(7, 10, "recent", staff, session)
    item = recent["items"][0]
    assert item["workspace"] == organization.name
    assert item["error"] == "Timed out"
    # The customer's payload is deliberately not part of this view.
    assert "input" not in item
    assert "payload" not in item


async def test_old_failures_fall_outside_the_window(
    session: AsyncSession, staff: User, organization: Organization
):
    await seed_runs(session, organization, failed=1)
    old = datetime.now(UTC) - timedelta(days=40)
    rows = (await session.execute(select(NodeExecution))).scalars().all()
    for row in rows:
        row.started_at = old
    await session.commit()

    grouped = await read_errors(7, 20, "grouped", staff, session)
    assert grouped["items"] == []


async def test_workspaces_are_listed_busiest_first(
    session: AsyncSession, staff: User, organization: Organization
):
    quiet = Organization(name="Quiet", slug=f"quiet-{uuid.uuid4().hex[:6]}")
    session.add(quiet)
    await session.commit()
    await seed_runs(session, organization, succeeded=4)
    await seed_runs(session, quiet, succeeded=1)

    listed = await read_workspaces(30, 50, staff, session)
    names = [item["name"] for item in listed["items"]]
    assert names[0] == organization.name
    assert listed["items"][0]["runs"] == 4
    assert listed["items"][0]["plan"] == "free"


def test_an_error_signature_hides_the_parts_that_differ():
    first = stats.signature("Run 4f6c2d3e-0a1b-4c5d-8e9f-0a1b2c3d4e5f failed after 1200 ms")
    second = stats.signature("Run 11111111-2222-3333-4444-555555555555 failed after 900 ms")
    assert first == second
