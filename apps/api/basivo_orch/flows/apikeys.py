"""API keys for calling published flows from another backend.

Section 4's request/response mode is for servers: cron jobs, Lambdas,
backend-to-backend calls. None of them can hold a cookie jar or complete a 2FA
challenge, so the session auth the web app uses cannot serve them.

Keys are organisation-scoped, so a leaked key exposes exactly one tenant and
can be revoked without touching anyone's password. They are stored as a SHA-256
digest — a plain digest is right here and would be wrong for a password: the
input is 256 bits from the OS CSPRNG, so there is no dictionary to attack and
nothing a work factor would buy.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.db import get_async_session
from basivo_orch.flows.models import ApiKey

#: Visible in logs and dashboards, so a leaked key is recognisable at a glance
#: and secret scanners have something to match on.
KEY_PREFIX = "bsv_"
KEY_BYTES = 32


def generate_key() -> tuple[str, str, str]:
    """Return (full key, stored prefix, hash)."""
    body = secrets.token_urlsafe(KEY_BYTES)
    key = f"{KEY_PREFIX}{body}"
    return key, key[: len(KEY_PREFIX) + 6], hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def resolve_api_key(session: AsyncSession, presented: str) -> ApiKey | None:
    """Look up a key by hash, or return None.

    The lookup is by digest, so the plaintext is never compared in the database
    and never appears in a query log. The final comparison is constant-time:
    the digest is indexed, and an attacker who could time the comparison could
    otherwise recover it one byte at a time.
    """
    if not presented.startswith(KEY_PREFIX):
        return None

    digest = hash_key(presented)
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
    record = result.scalar_one_or_none()

    if record is None or not hmac.compare_digest(record.key_hash, digest):
        return None
    if not record.is_usable:
        return None
    return record


class ApiCaller:
    """An authenticated external caller. Carries a tenant, never a user."""

    __slots__ = ("organization_id", "key_id")

    def __init__(self, organization_id: uuid.UUID, key_id: uuid.UUID) -> None:
        self.organization_id = organization_id
        self.key_id = key_id


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> ApiCaller:
    """Authenticate an external caller.

    Accepts `Authorization: Bearer bsv_…` or `X-API-Key: bsv_…`. Both are in
    common use and rejecting one just means a support ticket.
    """
    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Provide an API key in the Authorization or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    record = await resolve_api_key(session, presented)
    if record is None:
        # One message for unknown, revoked and expired. Distinguishing them
        # tells an attacker holding an old key whether it merely lapsed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That API key is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Best-effort: useful for spotting keys nobody uses any more, not worth
    # failing a request over, and deliberately not awaited on the hot path of
    # every call beyond this single UPDATE.
    record.last_used_at = datetime.now(UTC)
    await session.commit()

    return ApiCaller(organization_id=record.organization_id, key_id=record.id)
