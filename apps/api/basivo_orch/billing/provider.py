"""Dodo Payments: the two calls we make, and the deliveries we accept.

Dodo is a merchant of record, which is the reason it is here rather than a
gateway: it is the legal seller, so it charges GST in India and VAT elsewhere,
and it pays out to an Indian bank account. None of that tax logic is ours to
write, and none of it is in this file.

Signatures follow the Standard Webhooks scheme: the signed content is
`{webhook-id}.{webhook-timestamp}.{raw body}`, the key is the base64 body of a
`whsec_` secret, and the header carries a space-separated list of versioned
signatures so a secret can be rotated without dropping a delivery.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from basivo_orch.config import get_settings
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: How far apart the provider's clock and ours may be. A replayed delivery from
#: last week must not be accepted just because its signature is still valid.
TOLERANCE_SECONDS = 300

TIMEOUT = httpx.Timeout(15.0, read=30.0)


class ProviderError(Exception):
    """The provider could not be reached, or refused the request."""


class SignatureError(Exception):
    """A delivery did not prove it came from the provider."""


def base_url() -> str:
    settings = get_settings()
    return (
        "https://test.dodopayments.com"
        if settings.billing_is_test
        else "https://live.dodopayments.com"
    )


def plan_for_product(product_id: str) -> str | None:
    """Which plan a provider product means, or None when it is not ours."""
    settings = get_settings()
    for plan in ("pro", "team"):
        if product_id and settings.product_id(plan) == product_id:
            return plan
    return None


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


async def _post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.DODO_API_KEY:
        raise ProviderError("No payment provider is configured for this deployment.")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                f"{base_url()}{path}",
                json=body or {},
                headers={"Authorization": f"Bearer {settings.DODO_API_KEY}"},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(
            "The payment service could not be reached. Try again in a moment."
        ) from exc

    if response.status_code >= 400:
        # The provider's own message can name a card decline or a product that
        # does not exist. It is logged, never shown: it is written for us.
        log.warning(
            "billing.provider_refused",
            path=path,
            status=response.status_code,
            body=response.text[:400],
        )
        raise ProviderError("The payment service refused that request. Try again in a moment.")

    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("The payment service sent a reply we could not read.") from exc
    return payload if isinstance(payload, dict) else {}


async def create_checkout(
    *,
    product_id: str,
    organization_id: str,
    plan: str,
    email: str,
    name: str,
    return_url: str,
) -> str:
    """Start a hosted checkout and return the URL to send the customer to.

    The workspace id travels in `metadata` because that is what comes back on
    every subscription event afterwards. Matching on the email instead would
    put the wrong workspace on a plan the first time somebody pays with a
    different address than they signed up with.
    """
    payload = await _post(
        "/checkouts",
        {
            "product_cart": [{"product_id": product_id, "quantity": 1}],
            "customer": {"email": email, "name": name or email},
            "return_url": return_url,
            "metadata": {"organization_id": organization_id, "plan": plan},
        },
    )
    url = str(payload.get("checkout_url") or "")
    if not url:
        raise ProviderError("The payment service did not return a checkout page.")
    return url


async def customer_portal(customer_id: str) -> str:
    """A link where the customer manages the subscription they already have."""
    payload = await _post(f"/customers/{customer_id}/customer-portal/session")
    link = str(payload.get("link") or payload.get("url") or "")
    if not link:
        raise ProviderError("The payment service did not return a billing page.")
    return link


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


def _key(secret: str) -> bytes:
    """The signing key from a configured secret.

    Standard Webhooks secrets are `whsec_` plus base64. A secret pasted without
    the prefix is used as-is rather than refused: half the dashboards show it
    one way and half the other, and failing every delivery over that would look
    like an outage.
    """
    raw = secret.strip()
    if raw.startswith("whsec_"):
        try:
            return base64.b64decode(raw[len("whsec_") :])
        except Exception:  # noqa: BLE001 - a malformed secret is a config error
            return raw[len("whsec_") :].encode("utf-8")
    return raw.encode("utf-8")


def verify_signature(
    *,
    payload: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: datetime | None = None,
) -> None:
    """Raise `SignatureError` unless this delivery really came from Dodo.

    Checked on the RAW body. Re-serialising the parsed JSON first would change
    key order and whitespace, and every signature would fail.
    """
    if not secret.strip():
        raise SignatureError("No webhook secret is configured.")

    lower = {key.lower(): value for key, value in headers.items()}
    message_id = lower.get("webhook-id", "")
    timestamp = lower.get("webhook-timestamp", "")
    signature_header = lower.get("webhook-signature", "")
    if not message_id or not timestamp or not signature_header:
        raise SignatureError("This delivery is not signed.")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("This delivery has no readable timestamp.") from exc

    seconds = abs(int((now or datetime.now(UTC)).timestamp()) - sent_at)
    if seconds > TOLERANCE_SECONDS:
        raise SignatureError("This delivery is too old to accept.")

    signed = f"{message_id}.{timestamp}.".encode() + payload
    expected = base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest()).decode()

    for candidate in signature_header.split(" "):
        _, _, value = candidate.partition(",")
        if value and hmac.compare_digest(value, expected):
            return
    raise SignatureError("The signature on this delivery does not match.")


@dataclass(frozen=True, slots=True)
class Event:
    """One provider delivery, reduced to the fields that change a plan."""

    type: str
    timestamp: datetime | None
    subscription_id: str
    customer_id: str
    product_id: str
    status: str
    next_billing_date: datetime | None
    cancel_at_next_billing_date: bool
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def organization_id(self) -> str:
        return str(self.metadata.get("organization_id") or "")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_event(body: Any) -> Event:
    """Read a delivery. Anything missing becomes empty, never an exception.

    A payload shape that changed upstream must not take the endpoint down: an
    event we cannot read is one we ignore and log, and the provider's retry is
    then pointless rather than harmful.
    """
    if not isinstance(body, dict):
        raise SignatureError("This delivery has no readable body.")

    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    customer = data.get("customer")
    customer = customer if isinstance(customer, dict) else {}
    metadata = data.get("metadata") or body.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return Event(
        type=str(body.get("type") or ""),
        timestamp=_time(body.get("timestamp")),
        subscription_id=str(data.get("subscription_id") or ""),
        customer_id=str(customer.get("customer_id") or data.get("customer_id") or ""),
        product_id=str(data.get("product_id") or ""),
        status=str(data.get("status") or ""),
        next_billing_date=_time(data.get("next_billing_date")),
        cancel_at_next_billing_date=bool(data.get("cancel_at_next_billing_date")),
        metadata={str(key): str(value) for key, value in metadata.items()},
    )
