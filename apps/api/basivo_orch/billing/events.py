"""Turning a provider delivery into a plan.

Deliberately small and deliberately forgiving: a payment provider retries,
reorders and occasionally sends an event about something we never issued. None
of those may change a workspace's plan by accident, and none of them may make
the endpoint answer anything but 200 once the signature has been checked. A
delivery we refuse is a delivery that comes back every few minutes forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Organization
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.billing.plans import PLANS
from basivo_orch.billing.provider import Event, plan_for_product
from basivo_orch.config import get_settings
from basivo_orch.logging import get_logger

log = get_logger(__name__)


#: A payment failed. The plan holds for this long before falling back to Free.
def grace_period() -> timedelta:
    return timedelta(days=max(0, get_settings().BILLING_GRACE_DAYS))


ACTIVATING = ("subscription.active", "subscription.renewed", "subscription.plan_changed")
HOLDING = ("subscription.on_hold", "subscription.failed", "payment.failed")
ENDING = ("subscription.cancelled",)
EXPIRING = ("subscription.expired",)


async def _organization_for(session: AsyncSession, event: Event) -> uuid.UUID | None:
    """Which workspace this event is about.

    Three ways, in order of how much we trust them: the id we put in the
    checkout metadata ourselves, the subscription we already recorded, and the
    customer we already recorded. The email is deliberately not one of them:
    people pay with a different address than they sign up with, and matching on
    it would upgrade a stranger.
    """
    raw = event.organization_id
    if raw:
        try:
            candidate = uuid.UUID(raw)
        except ValueError:
            candidate = None
        if candidate is not None:
            exists = await session.get(Organization, candidate)
            if exists is not None:
                return candidate

    for column, value in (
        (Subscription.provider_subscription_id, event.subscription_id),
        (Subscription.provider_customer_id, event.customer_id),
    ):
        if not value:
            continue
        found = (
            await session.execute(select(Subscription).where(column == value))
        ).scalar_one_or_none()
        if found is not None:
            return found.organization_id
    return None


def _plan_for(event: Event, current: str | None) -> str | None:
    """The plan this event puts the workspace on, or None when it cannot say."""
    by_product = plan_for_product(event.product_id)
    if by_product:
        return by_product
    from_metadata = event.metadata.get("plan")
    if from_metadata in PLANS and from_metadata != "free":
        return from_metadata
    return current if current in PLANS and current != "free" else None


async def apply_event(
    session: AsyncSession, event: Event, *, now: datetime | None = None
) -> tuple[uuid.UUID | None, str]:
    """Apply one delivery. Returns (workspace, what was decided).

    The caller commits. Nothing here raises for a payload it does not
    understand: the outcome string says what happened and the delivery is
    recorded either way, so an event that changed nothing can still be found
    afterwards.
    """
    now = now or datetime.now(UTC)
    organization_id = await _organization_for(session, event)
    if organization_id is None:
        return None, "no workspace matched this event"

    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.organization_id == organization_id)
        )
    ).scalar_one_or_none()

    if subscription is None:
        if event.type not in ACTIVATING:
            # Nothing to cancel or suspend. Creating a row here would invent a
            # subscription out of an event about one that is already over.
            return organization_id, f"ignored {event.type}: this workspace has no subscription"
        subscription = Subscription(
            organization_id=organization_id,
            plan="free",
            status=SubscriptionStatus.EXPIRED,
        )
        session.add(subscription)

    # Out of order. Providers retry, and a retry of an older event arriving
    # after a newer one would otherwise resurrect a plan the customer left.
    last = subscription.last_event_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if event.timestamp and last and event.timestamp < last:
        return organization_id, f"ignored {event.type}: older than the last event applied"

    outcome = f"ignored {event.type}"

    if event.type in ACTIVATING:
        plan = _plan_for(event, subscription.plan)
        if plan is None:
            outcome = f"ignored {event.type}: product {event.product_id or 'unknown'} is not a plan"
        else:
            subscription.plan = plan
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.grace_until = None
            subscription.cancel_at_period_end = event.cancel_at_next_billing_date
            if event.next_billing_date:
                subscription.current_period_end = event.next_billing_date
            outcome = f"{plan} is active"

    elif event.type in HOLDING:
        if subscription.status == SubscriptionStatus.ACTIVE:
            subscription.status = SubscriptionStatus.PAST_DUE
            subscription.grace_until = now + grace_period()
            until = f"{subscription.grace_until:%Y-%m-%d}"
            outcome = f"payment failed, {subscription.plan} held until {until}"
        else:
            outcome = f"ignored {event.type}: subscription is {subscription.status}"

    elif event.type in ENDING:
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.cancel_at_period_end = True
        if event.next_billing_date:
            subscription.current_period_end = event.next_billing_date
        outcome = "cancelled, plan runs to the end of the paid period"

    elif event.type in EXPIRING:
        subscription.status = SubscriptionStatus.EXPIRED
        subscription.grace_until = None
        subscription.current_period_end = event.next_billing_date or subscription.current_period_end
        outcome = "subscription ended, back to the free plan"

    if event.subscription_id:
        subscription.provider_subscription_id = event.subscription_id
    if event.customer_id:
        subscription.provider_customer_id = event.customer_id
    if event.timestamp:
        subscription.last_event_at = event.timestamp

    log.info(
        "billing.event_applied",
        organization_id=str(organization_id),
        event_type=event.type,
        outcome=outcome,
    )
    return organization_id, outcome
