"""What the billing API sends and accepts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from basivo_orch.billing.plans import Plan


class PlanRead(BaseModel):
    code: str
    name: str
    tagline: str
    price_inr: str
    price_usd: str
    runs_per_month: int | None
    flows: int | None
    apps: int | None
    seats: int | None
    history_days: int
    storage_mb: int | None
    features: list[str]

    @classmethod
    def of(cls, plan: Plan) -> PlanRead:
        return cls(
            code=plan.code,
            name=plan.name,
            tagline=plan.tagline,
            price_inr=plan.price_inr,
            price_usd=plan.price_usd,
            runs_per_month=plan.runs_per_month,
            flows=plan.flows,
            apps=plan.apps,
            seats=plan.seats,
            history_days=plan.history_days,
            storage_mb=plan.storage_mb,
            features=list(plan.features),
        )


class UsageRead(BaseModel):
    runs_used: int
    runs_limit: int | None
    flows_used: int
    flows_limit: int | None
    apps_used: int
    apps_limit: int | None
    seats_used: int
    seats_limit: int | None
    #: Files and app builds, in megabytes, because a workspace reads its own
    #: storage in the units its plan is written in. One decimal: a workspace
    #: holding a few hundred kilobytes has not used "0 MB", and a meter that
    #: says it has looks broken to the person who just built something.
    storage_used_mb: float
    storage_limit_mb: int | None
    history_days: int


class BillingOverview(BaseModel):
    """Everything the billing page draws, in one request."""

    #: "demo" or "production". In demo nothing is charged and no limit applies.
    mode: str
    plan: PlanRead
    status: str
    usage: UsageRead
    current_period_end: datetime | None
    grace_until: datetime | None
    cancel_at_period_end: bool
    #: True when this workspace has been through checkout, so the page can
    #: offer to manage the subscription rather than to start one.
    can_manage: bool
    plans: list[PlanRead]


class CheckoutRequest(BaseModel):
    plan: str = Field(description="The plan code to subscribe to, such as 'pro'.")


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str
