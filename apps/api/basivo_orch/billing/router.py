"""Billing routes.

Three for the console and one for the provider. The console routes are
org-scoped through the same `require()` chokepoint every other route uses; the
provider route is authenticated by the signature on the request body and by
nothing else, which is why it lives outside the versioned prefix alongside the
other machine-to-machine hooks.

In demo mode the plans are a preview: the overview still answers, so the page
can show what the product will cost, and everything that would move money is
refused before a provider is ever called.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.auth.security.ratelimit import limiter
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.billing import service
from basivo_orch.billing.events import apply_event
from basivo_orch.billing.models import BillingEvent
from basivo_orch.billing.plans import PAID_PLANS, PLAN_ORDER, PLANS
from basivo_orch.billing.pricing import catalogue, product_id
from basivo_orch.billing.provider import (
    ProviderError,
    SignatureError,
    create_checkout,
    customer_portal,
    parse_event,
    verify_signature,
)
from basivo_orch.billing.schemas import (
    BillingOverview,
    CheckoutRequest,
    CheckoutResponse,
    PlanRead,
    PortalResponse,
    UsageRead,
)
from basivo_orch.config import get_settings
from basivo_orch.db import get_async_session
from basivo_orch.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["billing"])
webhook_router = APIRouter(tags=["billing"])

DEMO_MESSAGE = (
    "Billing is switched off in this deployment, so the plans are a preview "
    "and nothing can be charged."
)


def _live_or_refuse() -> None:
    if not get_settings().billing_is_live:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, DEMO_MESSAGE)


@router.get("/orgs/{organization_id}/billing", response_model=BillingOverview)
async def read_billing(
    context: OrgContext = Depends(require(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> BillingOverview:
    """The current plan, what has been used against it, and what else is sold."""
    state = await service.entitlement(session, context.organization_id)
    plan = state.plan
    return BillingOverview(
        mode=state.mode,
        plan=PlanRead.of(plan),
        status=state.status,
        usage=UsageRead(
            runs_used=state.runs_used,
            runs_limit=plan.runs_per_month,
            flows_used=state.flows_used,
            flows_limit=plan.flows,
            apps_used=state.apps_used,
            apps_limit=plan.apps,
            seats_used=state.seats_used,
            seats_limit=plan.seats,
            storage_used_mb=round(state.storage_used_bytes / (1024 * 1024)),
            storage_limit_mb=plan.storage_mb,
            history_days=plan.history_days,
        ),
        current_period_end=state.current_period_end,
        grace_until=state.grace_until,
        cancel_at_period_end=state.cancel_at_period_end,
        can_manage=state.has_provider_customer,
        plans=[
            PlanRead.of(plan)
            for code, plan in (await catalogue(session)).items()
            if code in PLAN_ORDER
        ],
    )


@router.post("/orgs/{organization_id}/billing/checkout", response_model=CheckoutResponse)
@limiter.limit("20/hour")
async def start_checkout(
    request: Request,
    response: Response,
    payload: CheckoutRequest,
    context: OrgContext = Depends(require(Permission.BILLING_MANAGE)),
    session: AsyncSession = Depends(get_async_session),
) -> CheckoutResponse:
    """Send the customer to the provider's hosted checkout."""
    _live_or_refuse()

    plan = payload.plan.strip().lower()
    if plan not in PAID_PLANS:
        sellable = ", ".join(PLANS[code].name for code in PAID_PLANS)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"There is no paid plan called {payload.plan!r}. Choose one of: {sellable}.",
        )

    product = await product_id(session, plan)
    if not product:
        # Cannot happen with a validated production config; this is the guard
        # for a plan added to the catalogue before its product exists.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "That plan is not on sale yet. Please get in touch and we will set it up.",
        )

    current = await service.entitlement(session, context.organization_id)
    if current.plan.code == plan and current.status == "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This workspace is already on the {PLANS[plan].name} plan.",
        )

    console = str(get_auth_settings().frontend_base_url).rstrip("/")
    try:
        url = await create_checkout(
            product_id=product,
            organization_id=str(context.organization_id),
            plan=plan,
            email=context.user.email,
            name=context.organization.name,
            return_url=f"{console}/app/billing?checkout=done",
        )
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return CheckoutResponse(checkout_url=url)


@router.post("/orgs/{organization_id}/billing/portal", response_model=PortalResponse)
@limiter.limit("30/hour")
async def open_portal(
    request: Request,
    response: Response,
    context: OrgContext = Depends(require(Permission.BILLING_MANAGE)),
    session: AsyncSession = Depends(get_async_session),
) -> PortalResponse:
    """Where the customer changes a card, downloads invoices, or cancels."""
    _live_or_refuse()

    subscription = await service.get_subscription(session, context.organization_id)
    if subscription is None or not subscription.provider_customer_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This workspace has never subscribed, so there is nothing to manage yet.",
        )
    try:
        link = await customer_portal(subscription.provider_customer_id)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return PortalResponse(portal_url=link)


@webhook_router.post("/billing/webhook", include_in_schema=False)
async def provider_webhook(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, str]:
    """Deliveries from the payment provider.

    Answers 200 for everything it has authenticated, including events it does
    not act on. A provider retries any other status until it succeeds, so
    refusing an event we simply do not care about would mean receiving it
    forever.
    """
    settings = get_settings()
    if not settings.billing_is_live:
        # A demo deployment has no subscriptions to move. Answering anything
        # other than "no such endpoint" would invite a live provider to keep
        # delivering to a box that can never act on it.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    raw = await request.body()
    try:
        verify_signature(payload=raw, headers=request.headers, secret=settings.DODO_WEBHOOK_SECRET)
    except SignatureError as exc:
        log.warning("billing.webhook_rejected", reason=str(exc))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature.") from exc

    delivery_id = request.headers.get("webhook-id", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a body that is not JSON is the provider's problem
        log.warning("billing.webhook_unreadable", delivery=delivery_id)
        return {"status": "ignored"}

    try:
        event = parse_event(body)
    except SignatureError:
        return {"status": "ignored"}

    # Recorded first and by the provider's own delivery id, so a redelivery
    # collides here and never reaches the subscription at all.
    record = BillingEvent(event_id=delivery_id, event_type=event.type, payload=body)
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info("billing.webhook_duplicate", delivery=delivery_id, event_type=event.type)
        return {"status": "duplicate"}

    organization_id, outcome = await apply_event(session, event)
    record.organization_id = organization_id
    record.outcome = outcome
    await session.commit()
    return {"status": "ok"}
