"""The console's billing routes.

Called directly with a constructed `OrgContext`, the same way the credential
tests do it: `Depends(require(...))` is an ordinary Python dependency, so
calling the endpoint function exercises the authority check the real request
path runs.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import Role
from basivo_orch.auth.models import Organization
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.billing.provider import ProviderError
from basivo_orch.billing.router import open_portal, read_billing, start_checkout
from basivo_orch.billing.schemas import CheckoutRequest
from tests.billing.conftest import make_context


class Nothing:
    """Stands in for the Request and Response the rate limiter wants."""

    headers: dict[str, str] = {}


async def test_the_overview_shows_the_free_plan_and_the_catalogue(
    session: AsyncSession, organization: Organization
):
    view = await read_billing(context=make_context(organization), session=session)
    assert view.mode == "production"
    assert view.plan.code == "free"
    assert view.usage.runs_used == 0
    assert view.usage.runs_limit == 100
    assert [plan.code for plan in view.plans] == ["free", "pro", "team"]
    assert view.can_manage is False


async def test_the_overview_says_so_in_demo_mode(
    session: AsyncSession, organization: Organization, demo_mode: None
):
    view = await read_billing(context=make_context(organization), session=session)
    assert view.mode == "demo"
    assert view.usage.runs_limit is None


async def test_a_viewer_may_read_the_plan(session: AsyncSession, organization: Organization):
    """A limit nobody can see is a limit that looks like a bug."""
    view = await read_billing(context=make_context(organization, Role.VIEWER), session=session)
    assert view.plan.code == "free"


async def test_an_admin_may_not_spend_the_owners_money(
    session: AsyncSession, organization: Organization
):
    from basivo_orch.auth.authz import Permission

    context = make_context(organization, Role.ADMIN)
    assert Permission.BILLING_READ in context.permissions
    assert Permission.BILLING_MANAGE not in context.permissions


async def test_checkout_is_refused_while_billing_is_switched_off(
    session: AsyncSession, organization: Organization, demo_mode: None
):
    with pytest.raises(HTTPException) as raised:
        await start_checkout(
            Nothing(),  # type: ignore[arg-type]
            Nothing(),  # type: ignore[arg-type]
            CheckoutRequest(plan="pro"),
            context=make_context(organization),
            session=session,
        )
    assert raised.value.status_code == 400
    assert "preview" in raised.value.detail


async def test_a_plan_that_is_not_sold_is_refused(
    session: AsyncSession, organization: Organization
):
    for plan in ("free", "platinum"):
        with pytest.raises(HTTPException) as raised:
            await start_checkout(
                Nothing(),  # type: ignore[arg-type]
                Nothing(),  # type: ignore[arg-type]
                CheckoutRequest(plan=plan),
                context=make_context(organization),
                session=session,
            )
        assert raised.value.status_code == 422


async def test_paying_twice_for_the_same_plan_is_refused(
    session: AsyncSession, organization: Organization
):
    session.add(
        Subscription(
            organization_id=organization.id,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as raised:
        await start_checkout(
            Nothing(),  # type: ignore[arg-type]
            Nothing(),  # type: ignore[arg-type]
            CheckoutRequest(plan="pro"),
            context=make_context(organization),
            session=session,
        )
    assert raised.value.status_code == 409


async def test_checkout_sends_the_workspace_id_to_the_provider(
    session: AsyncSession, organization: Organization, monkeypatch: pytest.MonkeyPatch
):
    """The id in the metadata is what every later event is matched on."""
    seen: dict = {}

    async def fake_checkout(**kwargs):
        seen.update(kwargs)
        return "https://checkout.example/session"

    monkeypatch.setattr("basivo_orch.billing.router.create_checkout", fake_checkout)

    result = await start_checkout(
        Nothing(),  # type: ignore[arg-type]
        Nothing(),  # type: ignore[arg-type]
        CheckoutRequest(plan="pro"),
        context=make_context(organization),
        session=session,
    )
    assert result.checkout_url == "https://checkout.example/session"
    assert seen["organization_id"] == str(organization.id)
    assert seen["product_id"] == "pdt_pro"
    assert seen["return_url"].endswith("/app/billing?checkout=done")


async def test_a_provider_outage_is_reported_as_one(
    session: AsyncSession, organization: Organization, monkeypatch: pytest.MonkeyPatch
):
    async def fake_checkout(**_):
        raise ProviderError("The payment service could not be reached. Try again in a moment.")

    monkeypatch.setattr("basivo_orch.billing.router.create_checkout", fake_checkout)

    with pytest.raises(HTTPException) as raised:
        await start_checkout(
            Nothing(),  # type: ignore[arg-type]
            Nothing(),  # type: ignore[arg-type]
            CheckoutRequest(plan="team"),
            context=make_context(organization),
            session=session,
        )
    assert raised.value.status_code == 502
    assert "could not be reached" in raised.value.detail


async def test_the_portal_needs_a_subscription_first(
    session: AsyncSession, organization: Organization
):
    with pytest.raises(HTTPException) as raised:
        await open_portal(
            Nothing(),  # type: ignore[arg-type]
            Nothing(),  # type: ignore[arg-type]
            context=make_context(organization),
            session=session,
        )
    assert raised.value.status_code == 404


async def test_the_portal_opens_for_a_customer_we_have(
    session: AsyncSession, organization: Organization, monkeypatch: pytest.MonkeyPatch
):
    session.add(
        Subscription(
            organization_id=organization.id,
            plan="pro",
            status=SubscriptionStatus.ACTIVE,
            provider_customer_id="cus_9",
        )
    )
    await session.commit()

    async def fake_portal(customer_id: str) -> str:
        assert customer_id == "cus_9"
        return "https://portal.example/cus_9"

    monkeypatch.setattr("basivo_orch.billing.router.customer_portal", fake_portal)

    result = await open_portal(
        Nothing(),  # type: ignore[arg-type]
        Nothing(),  # type: ignore[arg-type]
        context=make_context(organization),
        session=session,
    )
    assert result.portal_url == "https://portal.example/cus_9"
