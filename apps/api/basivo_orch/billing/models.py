"""Tables for subscriptions and the webhook events that move them.

Two rows per workspace at most: what it is paying for, and which provider
events have already been applied. The second table is not bookkeeping for its
own sake — a payment provider retries a delivery until it gets a 200, and
without a record of what was already handled a retry a week later would
re-apply a state the workspace has since left.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from basivo_orch.db import Base
from basivo_orch.flows.models import JSONColumn


class SubscriptionStatus:
    """What the provider last told us, not what we would like to be true.

    The effective plan is computed from this plus the clock (see
    ``billing.service.effective_plan``); it is never written here, so a
    grace period that expires needs no job to run for the limits to apply.
    """

    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    ALL = (ACTIVE, PAST_DUE, CANCELLED, EXPIRED)


class Subscription(Base):
    """One row per workspace. Absent means the workspace is on the free plan."""

    __tablename__ = "subscription"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), unique=True, index=True
    )

    plan: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))

    provider: Mapped[str] = mapped_column(String(24), default="dodo")
    provider_customer_id: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(128), default=None, index=True
    )

    #: When the paid period ends. A cancelled subscription keeps its plan until
    #: this passes, because the customer paid for that period.
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Set when a payment fails. The plan holds until it passes, then the
    #: workspace falls back to Free. Data is never deleted for non-payment.
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False)

    #: The timestamp of the last provider event applied to this row. Events
    #: that arrive out of order (a retry of "active" after a "cancelled") are
    #: ignored by comparing against this instead of trusting arrival order.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BillingEvent(Base):
    """Every provider delivery we accepted, keyed by the provider's own id.

    The unique constraint is the whole point: a redelivery inserts nothing and
    is answered 200 without touching the subscription.
    """

    __tablename__ = "billing_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(24), default="dodo")
    #: The provider's delivery id (`webhook-id`), unique per delivery.
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), default=None, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict)
    #: What the handler decided, so an unexplained plan change can be traced
    #: back to the delivery that caused it.
    outcome: Mapped[str] = mapped_column(Text(), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanOverride(Base):
    """A plan whose numbers were changed from the console.

    Prices move, and a price that can only move by editing Python and
    redeploying is a price nobody dares to move. Every column is nullable and
    means "leave the built-in value alone"; a column that is set replaces it.

    ``UNLIMITED_SENTINEL`` is how "no limit" is written down, because NULL is
    already spoken for by "not overridden".

    A price here is what the console SHOWS. What a customer is actually
    charged lives in the payment provider, so changing a price means creating
    the new product there and putting its id in ``product_id``.
    """

    __tablename__ = "plan_override"

    #: The plan code from `billing.plans`, such as "pro".
    code: Mapped[str] = mapped_column(String(32), primary_key=True)

    name: Mapped[str | None] = mapped_column(String(64), default=None)
    tagline: Mapped[str | None] = mapped_column(String(200), default=None)
    price_inr: Mapped[str | None] = mapped_column(String(32), default=None)
    price_usd: Mapped[str | None] = mapped_column(String(32), default=None)

    runs_per_month: Mapped[int | None] = mapped_column(default=None)
    flows: Mapped[int | None] = mapped_column(default=None)
    seats: Mapped[int | None] = mapped_column(default=None)
    history_days: Mapped[int | None] = mapped_column(default=None)

    features: Mapped[list[str] | None] = mapped_column(JSONColumn, default=None)
    #: The provider product to send a customer to. Overrides the environment.
    product_id: Mapped[str | None] = mapped_column(String(128), default=None)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


#: Written into a numeric column to mean "no limit". NULL cannot mean it: NULL
#: already means "this field was not overridden".
UNLIMITED_SENTINEL = -1
