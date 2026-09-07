"""Provider deliveries: proving they are real, and applying them once.

A payment provider retries until it gets a 200, sends events out of order, and
occasionally sends one about a subscription we never issued. None of those may
move a workspace onto a plan it did not pay for, and none of them may make the
endpoint answer anything a provider will retry forever.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Organization
from basivo_orch.billing.events import apply_event
from basivo_orch.billing.models import BillingEvent, Subscription, SubscriptionStatus
from basivo_orch.billing.provider import SignatureError, parse_event, verify_signature
from basivo_orch.billing.router import provider_webhook
from basivo_orch.billing.service import effective_plan

SECRET = "whsec_dGVzdC1zZWNyZXQ="  # noqa: S105 - a test fixture, not a real secret
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def sign(body: bytes, *, secret: str = SECRET, message_id: str = "msg_1", at: datetime = NOW):
    key = (
        base64.b64decode(secret.removeprefix("whsec_"))
        if secret.startswith("whsec_")
        else secret.encode()
    )
    stamp = str(int(at.timestamp()))
    digest = hmac.new(key, f"{message_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": message_id,
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + base64.b64encode(digest).decode(),
    }


def body_for(
    event_type: str,
    *,
    organization_id: str | None = None,
    product_id: str = "pdt_pro",
    subscription_id: str = "sub_1",
    customer_id: str = "cus_1",
    next_billing_date: str | None = "2026-07-15T12:00:00Z",
    timestamp: datetime = NOW,
) -> dict:
    data: dict = {
        "payload_type": "Subscription",
        "subscription_id": subscription_id,
        "product_id": product_id,
        "status": "active",
        "customer": {"customer_id": customer_id, "email": "buyer@example.com"},
        "next_billing_date": next_billing_date,
    }
    if organization_id:
        data["metadata"] = {"organization_id": organization_id, "plan": "pro"}
    return {"type": event_type, "timestamp": timestamp.isoformat(), "data": data}


# --- signatures ------------------------------------------------------------


def test_a_signed_delivery_is_accepted():
    raw = b'{"type":"subscription.active"}'
    verify_signature(payload=raw, headers=sign(raw), secret=SECRET, now=NOW)


def test_a_tampered_body_is_rejected():
    raw = b'{"type":"subscription.active"}'
    headers = sign(raw)
    with pytest.raises(SignatureError):
        verify_signature(payload=raw + b" ", headers=headers, secret=SECRET, now=NOW)


def test_an_unsigned_delivery_is_rejected():
    with pytest.raises(SignatureError, match="not signed"):
        verify_signature(payload=b"{}", headers={}, secret=SECRET, now=NOW)


def test_a_replayed_delivery_from_last_week_is_rejected():
    """The signature stays valid forever; the timestamp is what expires."""
    raw = b"{}"
    headers = sign(raw, at=NOW - timedelta(days=7))
    with pytest.raises(SignatureError, match="too old"):
        verify_signature(payload=raw, headers=headers, secret=SECRET, now=NOW)


def test_another_senders_secret_is_rejected():
    raw = b"{}"
    headers = sign(raw, secret="whsec_b3RoZXI=")
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(payload=raw, headers=headers, secret=SECRET, now=NOW)


def test_a_secret_pasted_without_its_prefix_still_works():
    """Dashboards show it both ways. Failing every delivery over that would
    look like an outage rather than a typo."""
    raw = b"{}"
    plain = "plain-secret"  # noqa: S105 - a test fixture
    verify_signature(payload=raw, headers=sign(raw, secret=plain), secret=plain, now=NOW)


def test_one_valid_signature_among_several_is_enough():
    """Rotating a secret means two are sent for a while."""
    raw = b"{}"
    headers = sign(raw)
    headers["webhook-signature"] = "v1,bm90LWl0 " + headers["webhook-signature"]
    verify_signature(payload=raw, headers=headers, secret=SECRET, now=NOW)


def test_an_empty_secret_never_accepts_anything():
    raw = b"{}"
    with pytest.raises(SignatureError):
        verify_signature(payload=raw, headers=sign(raw), secret="", now=NOW)


# --- applying an event -----------------------------------------------------


async def test_an_activation_puts_the_workspace_on_the_plan(
    session: AsyncSession, organization: Organization
):
    event = parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    org_id, outcome = await apply_event(session, event, now=NOW)
    await session.commit()

    assert org_id == organization.id
    assert "pro is active" == outcome
    row = (
        await session.execute(
            select(Subscription).where(Subscription.organization_id == organization.id)
        )
    ).scalar_one()
    assert row.plan == "pro"
    assert row.status == SubscriptionStatus.ACTIVE
    assert row.provider_customer_id == "cus_1"
    assert effective_plan(row, NOW).code == "pro"


async def test_a_failed_payment_holds_the_plan_for_the_grace_period(
    session: AsyncSession, organization: Organization
):
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    event = parse_event(
        body_for("payment.failed", organization_id=str(organization.id), timestamp=NOW)
    )
    _, outcome = await apply_event(session, event, now=NOW)
    await session.commit()

    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.status == SubscriptionStatus.PAST_DUE
    # SQLite gives the timestamp back without its timezone; the value is the
    # one that was written, and every comparison in the service reattaches UTC.
    assert row.grace_until.replace(tzinfo=UTC) == NOW + timedelta(days=7)
    assert "payment failed" in outcome
    # Still on the plan today, back to free once the grace period is over.
    assert effective_plan(row, NOW).code == "pro"
    assert effective_plan(row, NOW + timedelta(days=8)).code == "free"


async def test_a_renewal_after_a_failure_clears_the_grace_period(
    session: AsyncSession, organization: Organization
):
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    await apply_event(
        session,
        parse_event(body_for("payment.failed", organization_id=str(organization.id))),
        now=NOW,
    )
    await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.renewed",
                organization_id=str(organization.id),
                timestamp=NOW + timedelta(minutes=5),
            )
        ),
    )
    await session.commit()

    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.status == SubscriptionStatus.ACTIVE
    assert row.grace_until is None


async def test_a_cancellation_keeps_the_plan_to_the_end_of_the_period(
    session: AsyncSession, organization: Organization
):
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.cancelled",
                organization_id=str(organization.id),
                timestamp=NOW + timedelta(minutes=1),
            )
        ),
    )
    await session.commit()

    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.status == SubscriptionStatus.CANCELLED
    assert row.cancel_at_period_end is True
    assert effective_plan(row, NOW).code == "pro"
    assert effective_plan(row, datetime(2026, 8, 1, tzinfo=UTC)).code == "free"


async def test_an_expiry_returns_the_workspace_to_the_free_plan(
    session: AsyncSession, organization: Organization
):
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.expired",
                organization_id=str(organization.id),
                timestamp=NOW + timedelta(days=30),
            )
        ),
    )
    await session.commit()

    row = (await session.execute(select(Subscription))).scalar_one()
    assert effective_plan(row, NOW + timedelta(days=31)).code == "free"


async def test_an_older_event_arriving_late_is_ignored(
    session: AsyncSession, organization: Organization
):
    """A retry of "active" after a cancellation must not resurrect the plan."""
    await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.active",
                organization_id=str(organization.id),
                timestamp=NOW,
            )
        ),
    )
    await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.cancelled",
                organization_id=str(organization.id),
                timestamp=NOW + timedelta(hours=1),
            )
        ),
    )
    _, outcome = await apply_event(
        session,
        parse_event(
            body_for(
                "subscription.active",
                organization_id=str(organization.id),
                timestamp=NOW,
            )
        ),
    )
    await session.commit()

    assert "older than the last event" in outcome
    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.status == SubscriptionStatus.CANCELLED


async def test_an_event_for_a_workspace_we_do_not_have_changes_nothing(
    session: AsyncSession,
):
    event = parse_event(body_for("subscription.active", organization_id=str(uuid.uuid4())))
    org_id, outcome = await apply_event(session, event)
    assert org_id is None
    assert "no workspace" in outcome
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_a_cancellation_for_a_workspace_that_never_subscribed_creates_nothing(
    session: AsyncSession, organization: Organization
):
    event = parse_event(body_for("subscription.cancelled", organization_id=str(organization.id)))
    _, outcome = await apply_event(session, event)
    await session.commit()
    assert "has no subscription" in outcome
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_a_product_that_is_not_one_of_our_plans_is_ignored(
    session: AsyncSession, organization: Organization
):
    body = body_for("subscription.active", product_id="pdt_someone_elses")
    body["data"]["metadata"] = {"organization_id": str(organization.id)}
    _, outcome = await apply_event(session, parse_event(body))
    await session.commit()
    assert "is not a plan" in outcome


async def test_an_event_type_we_do_not_handle_is_ignored(
    session: AsyncSession, organization: Organization
):
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    _, outcome = await apply_event(
        session,
        parse_event(
            body_for(
                "dispute.opened",
                organization_id=str(organization.id),
                timestamp=NOW + timedelta(minutes=1),
            )
        ),
    )
    await session.commit()
    assert outcome.startswith("ignored")
    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.status == SubscriptionStatus.ACTIVE


async def test_a_later_event_without_metadata_is_matched_by_subscription_id(
    session: AsyncSession, organization: Organization
):
    """Only the checkout carries our metadata. Everything after it is matched
    on the ids we recorded, never on the payer's email."""
    await apply_event(
        session, parse_event(body_for("subscription.active", organization_id=str(organization.id)))
    )
    await session.commit()

    later = body_for("subscription.renewed", timestamp=NOW + timedelta(days=30))
    org_id, outcome = await apply_event(session, parse_event(later))
    assert org_id == organization.id
    assert "active" in outcome


# --- the endpoint ----------------------------------------------------------


class FakeRequest:
    """Enough of a Request for the handler: raw body, headers, json."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body

    async def json(self):
        return json.loads(self._body)


async def post(session: AsyncSession, body: dict, **kwargs):
    raw = json.dumps(body).encode()
    request = FakeRequest(raw, sign(raw, at=datetime.now(UTC), **kwargs))
    return await provider_webhook(request, session=session)  # type: ignore[arg-type]


async def test_the_endpoint_applies_a_signed_delivery(
    session: AsyncSession, organization: Organization
):
    result = await post(
        session, body_for("subscription.active", organization_id=str(organization.id))
    )
    assert result["status"] == "ok"
    row = (await session.execute(select(Subscription))).scalar_one()
    assert row.plan == "pro"


async def test_the_same_delivery_twice_is_applied_once(
    session: AsyncSession, organization: Organization
):
    """The provider retries when our reply is slow. The second delivery must
    be a no-op, not a second plan change."""
    body = body_for("subscription.active", organization_id=str(organization.id))
    assert (await post(session, body, message_id="msg_a"))["status"] == "ok"
    assert (await post(session, body, message_id="msg_a"))["status"] == "duplicate"

    events = (await session.execute(select(BillingEvent))).scalars().all()
    assert len(events) == 1


async def test_an_unsigned_delivery_is_refused_by_the_endpoint(session: AsyncSession):
    request = FakeRequest(b"{}", {})
    with pytest.raises(HTTPException) as raised:
        await provider_webhook(request, session=session)  # type: ignore[arg-type]
    assert raised.value.status_code == 401
    assert (await session.execute(select(BillingEvent))).scalars().all() == []


async def test_the_endpoint_does_not_exist_in_demo_mode(
    session: AsyncSession, organization: Organization, demo_mode: None
):
    """A demo box has no subscriptions to move, so it does not answer at all."""
    body = body_for("subscription.active", organization_id=str(organization.id))
    raw = json.dumps(body).encode()
    request = FakeRequest(raw, sign(raw, at=datetime.now(UTC)))
    with pytest.raises(HTTPException) as raised:
        await provider_webhook(request, session=session)  # type: ignore[arg-type]
    assert raised.value.status_code == 404
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_a_delivery_that_is_not_json_is_accepted_and_dropped(session: AsyncSession):
    """Answering anything else would have the provider retry it forever."""
    raw = b"not json at all"
    request = FakeRequest(raw, sign(raw, at=datetime.now(UTC)))
    result = await provider_webhook(request, session=session)  # type: ignore[arg-type]
    assert result["status"] == "ignored"


async def test_every_delivery_is_recorded_with_what_it_did(
    session: AsyncSession, organization: Organization
):
    await post(session, body_for("subscription.active", organization_id=str(organization.id)))
    event = (await session.execute(select(BillingEvent))).scalar_one()
    assert event.event_type == "subscription.active"
    assert event.organization_id == organization.id
    assert "pro is active" in event.outcome
