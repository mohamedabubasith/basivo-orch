"""What a workspace is allowed to do, and the one place that decides it.

Every limit is enforced here and called from the choke point the feature
already funnels through: `flows.service.create_run` for runs, `create_flow`
for flows, the invite route for seats. Enforcing at the routes instead would
mean the schedule ticker and the webhook path quietly ran for free, which is
exactly the shape of a billing bug nobody notices until the bill arrives.

Nothing in here charges anything. It reads a subscription row that the webhook
handler writes, and it answers one question: what may this workspace do right
now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Membership
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.billing.plans import PLANS, UNLIMITED, Plan, plan_or_free
from basivo_orch.billing.pricing import catalogue
from basivo_orch.config import get_settings
from basivo_orch.flows.models import Artifact, Flow, Run


class QuotaExceeded(Exception):
    """A limit of the current plan is in the way.

    Carries the sentence the customer reads. The routes turn it into 402,
    which is the only status a client can tell apart from "you are not allowed
    to do this at all" (403) and "something broke" (500).
    """

    def __init__(self, message: str, *, plan: str, limit_name: str) -> None:
        super().__init__(message)
        self.message = message
        self.plan = plan
        self.limit_name = limit_name


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres does not.

    Comparing a naive value against an aware `now` raises TypeError, which in a
    grace-period check would be an outage rather than a refusal.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def month_start(now: datetime | None = None) -> datetime:
    """The start of the current usage month, in UTC.

    A calendar month, not a rolling window from the subscription date: the
    number a customer can check against their own calendar is the one they
    will believe.
    """
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def effective_plan(
    subscription: Subscription | None,
    now: datetime | None = None,
    plans: dict[str, Plan] | None = None,
) -> Plan:
    """The plan whose limits apply this instant.

    Computed rather than stored, so a grace period that runs out or a cancelled
    period that ends needs no scheduled job: the next request sees the new
    answer. A job that had to run for a limit to apply is a job whose failure
    hands out a free upgrade.
    """
    plans = plans or PLANS
    free = plans.get("free", PLANS["free"])
    if subscription is None:
        return free

    now = now or datetime.now(UTC)
    plan = plans.get(subscription.plan) or plan_or_free(subscription.plan)
    status = subscription.status

    if status == SubscriptionStatus.ACTIVE:
        return plan
    if status == SubscriptionStatus.PAST_DUE:
        grace = _aware(subscription.grace_until)
        return plan if grace and now < grace else free
    if status == SubscriptionStatus.CANCELLED:
        # Paid for, so it is theirs until the period they bought is over.
        end = _aware(subscription.current_period_end)
        return plan if end and now < end else free
    return free


async def get_subscription(
    session: AsyncSession, organization_id: uuid.UUID
) -> Subscription | None:
    result = await session.execute(
        select(Subscription).where(Subscription.organization_id == organization_id)
    )
    return result.scalar_one_or_none()


async def current_plan(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> Plan:
    """The plan in force, honouring the billing switch.

    In demo mode this is the unlimited placeholder for every workspace, which
    is what makes every check below a no-op without any of them knowing about
    the switch.
    """
    if not get_settings().billing_is_live:
        return UNLIMITED
    return effective_plan(
        await get_subscription(session, organization_id),
        now,
        await catalogue(session),
    )


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


async def runs_this_month(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    # ponytail: counted from the run table, which cannot drift out of step with
    # reality. Move to a counter row if a workspace ever has enough runs for
    # this COUNT to show up in the query log.
    result = await session.execute(
        select(func.count())
        .select_from(Run)
        .where(Run.organization_id == organization_id, Run.created_at >= month_start(now))
    )
    return int(result.scalar_one())


async def flow_count(session: AsyncSession, organization_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Flow).where(Flow.organization_id == organization_id)
    )
    return int(result.scalar_one())


async def app_count(session: AsyncSession, organization_id: uuid.UUID) -> int:
    from basivo_orch.appbuilder.models import AppProject

    result = await session.execute(
        select(func.count())
        .select_from(AppProject)
        .where(AppProject.organization_id == organization_id)
    )
    return int(result.scalar_one())


async def storage_bytes(session: AsyncSession, organization_id: uuid.UUID) -> int:
    """Every byte this workspace has stored.

    Both tables that hold file bytes: artifacts, which is every rendered file
    and every app build, and the images people upload to an app. Counted
    rather than tracked in a counter row for the same reason runs are: a
    counter that drifts is worse than a query that is a little slower, and
    this one runs on an upload, not on a hot path.
    """
    from basivo_orch.appbuilder.models import AppAsset, AppProject

    files = await session.execute(
        select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(
            Artifact.organization_id == organization_id
        )
    )
    uploads = await session.execute(
        select(func.coalesce(func.sum(AppAsset.size_bytes), 0))
        .select_from(AppAsset)
        .join(AppProject, AppProject.id == AppAsset.project_id)
        .where(AppProject.organization_id == organization_id)
    )
    return int(files.scalar_one()) + int(uploads.scalar_one())


async def seat_count(session: AsyncSession, organization_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.organization_id == organization_id)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


async def check_run_quota(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Raise `QuotaExceeded` when this workspace has used its runs.

    Called after the idempotency lookup in `create_run`, never before: a
    provider retrying a delivery must get the run it already started, not a
    quota refusal for a run that was already counted.
    """
    plan = await current_plan(session, organization_id, now=now)
    if plan.runs_per_month is None:
        return
    # ponytail: two runs starting in the same instant can both read the count
    # before either is written, so a workspace can go one or two runs over.
    # That is the right trade here: the alternative is a lock on the hot path
    # of every run, to protect a number that is a fair-use limit and not money.
    used = await runs_this_month(session, organization_id, now=now)
    if used < plan.runs_per_month:
        return
    raise QuotaExceeded(
        f"This workspace has used all {plan.runs_per_month:,} runs on the {plan.name} plan "
        "this month. Upgrade to keep running, or wait for the next month to start.",
        plan=plan.code,
        limit_name="runs",
    )


async def check_flow_quota(session: AsyncSession, organization_id: uuid.UUID) -> None:
    plan = await current_plan(session, organization_id)
    if plan.flows is None:
        return
    used = await flow_count(session, organization_id)
    if used < plan.flows:
        return
    raise QuotaExceeded(
        f"The {plan.name} plan keeps {plan.flows} flows and this workspace has {used}. "
        "Upgrade for unlimited flows, or delete one you no longer need.",
        plan=plan.code,
        limit_name="flows",
    )


async def check_app_quota(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Raise `QuotaExceeded` when a workspace has all the apps its plan allows.

    Apps are capped separately from flows because they cost differently: each
    message runs a coding agent and a compiler, and each build is kept.
    """
    plan = await current_plan(session, organization_id)
    if plan.apps is None:
        return
    used = await app_count(session, organization_id)
    if used < plan.apps:
        return
    apps = "app" if plan.apps == 1 else "apps"
    raise QuotaExceeded(
        f"The {plan.name} plan keeps {plan.apps} {apps} and this workspace has {used}. "
        "Upgrade for more, or delete one you have finished with.",
        plan=plan.code,
        limit_name="apps",
    )


async def check_seat_quota(session: AsyncSession, organization_id: uuid.UUID) -> None:
    plan = await current_plan(session, organization_id)
    if plan.seats is None:
        return
    used = await seat_count(session, organization_id)
    if used < plan.seats:
        return
    seats = "seat" if plan.seats == 1 else "seats"
    raise QuotaExceeded(
        f"The {plan.name} plan includes {plan.seats} {seats} and all of them are taken. "
        "Upgrade to add more people.",
        plan=plan.code,
        limit_name="seats",
    )


async def check_storage_quota(
    session: AsyncSession, organization_id: uuid.UUID, *, adding: int = 0
) -> None:
    """Raise `QuotaExceeded` when storing `adding` more bytes would go over.

    Called wherever bytes are written: the node that saves an artifact and the
    route that accepts an upload. Disk is shared between every workspace on the
    deployment, so this is the one limit that is enforced before the write
    rather than counted after it.
    """
    plan = await current_plan(session, organization_id)
    if plan.storage_mb is None:
        return
    limit = plan.storage_mb * 1024 * 1024
    used = await storage_bytes(session, organization_id)
    if used + adding <= limit:
        return
    raise QuotaExceeded(
        f"This workspace has used {used / (1024 * 1024):.0f} MB of the {plan.storage_mb} MB "
        f"on the {plan.name} plan. Delete an app or an old file, or upgrade for more room.",
        plan=plan.code,
        limit_name="storage",
    )


async def history_cutoff(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> datetime | None:
    """The oldest run a workspace may list, or None for no limit.

    Runs older than this are still stored and still counted. They are not
    listed, because history length is what the bigger plans sell. Nothing is
    deleted for being on a smaller plan.
    """
    plan = await current_plan(session, organization_id, now=now)
    if plan.history_days >= 3_650:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=plan.history_days)


# ---------------------------------------------------------------------------
# What the billing page shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entitlement:
    mode: str
    plan: Plan
    status: str
    runs_used: int
    flows_used: int
    apps_used: int
    seats_used: int
    storage_used_bytes: int
    current_period_end: datetime | None
    grace_until: datetime | None
    cancel_at_period_end: bool
    #: True once the customer has been to checkout, which is what decides
    #: whether the page offers "Upgrade" or "Manage subscription".
    has_provider_customer: bool


async def entitlement(
    session: AsyncSession, organization_id: uuid.UUID, *, now: datetime | None = None
) -> Entitlement:
    live = get_settings().billing_is_live
    subscription = await get_subscription(session, organization_id) if live else None
    plan = effective_plan(subscription, now, await catalogue(session)) if live else UNLIMITED

    return Entitlement(
        # The mode itself, not a boolean: the console says something different
        # for a deployment rehearsing against test cards than for one taking
        # money, and "live or not" cannot express that.
        mode=get_settings().BILLING_MODE,
        plan=plan,
        status=subscription.status if subscription else SubscriptionStatus.ACTIVE,
        runs_used=await runs_this_month(session, organization_id, now=now),
        flows_used=await flow_count(session, organization_id),
        apps_used=await app_count(session, organization_id),
        seats_used=await seat_count(session, organization_id),
        storage_used_bytes=await storage_bytes(session, organization_id),
        current_period_end=_aware(subscription.current_period_end) if subscription else None,
        grace_until=_aware(subscription.grace_until) if subscription else None,
        cancel_at_period_end=bool(subscription.cancel_at_period_end) if subscription else False,
        has_provider_customer=bool(subscription and subscription.provider_customer_id),
    )
