"""Access and refresh token issuance, verification and rotation.

Two token types, deliberately different in kind:

**Access token** — a short-lived signed JWT. Stateless, so verifying it costs no
database round trip. The price of statelessness is that it cannot be revoked
before it expires, which is exactly why its lifetime is minutes.

**Refresh token** — a long-lived opaque random string. Only its SHA-256 digest
is stored. Every use rotates it: the presented token is marked used and a new
one is issued in the same *family*.

Rotation is what makes theft detectable. If an attacker exfiltrates a refresh
token and uses it, the legitimate client's copy is now stale. When the real
client next refreshes, it presents an already-used token — and that can only
happen if a token leaked. The response is to revoke the entire family, which
logs out the attacker and the victim, and raises an audit event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

import jwt
import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import RefreshToken, User
from basivo_orch.auth.security.crypto import random_token, sha256_hex
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)


class TokenType(StrEnum):
    """Purposes for the short-lived JWTs this module mints.

    Note what is *absent*: there is no ``ACCESS`` member. Access tokens are
    issued and validated by the engine's own JWT strategy
    (``app.auth.engine.backends``), and this module never mints one.

    That separation is load-bearing. Both sign with ``JWT_SECRET``, so if a
    token minted here carried the same ``aud`` as an access token, the engine
    would happily accept it — meaning a step-up token, which is supposed to
    represent "first factor done, second factor pending", would authenticate
    every API route. Binding each purpose to its own audience
    (``<aud>:step_up``, ``<aud>:magic_link``, …) makes that structurally
    impossible: the engine only accepts ``<aud>``, and :func:`decode_purpose_token`
    only accepts the exact purpose it was asked for.
    """

    RESET_PASSWORD = "reset_password"
    VERIFY_EMAIL = "verify_email"
    STEP_UP = "step_up"
    """Issued after a first factor succeeds but before 2FA completes. Carries no
    API authority — it may only be exchanged at the 2FA verification endpoint."""


class RevocationReason(StrEnum):
    ROTATED = "rotated"
    LOGOUT = "logout"
    REUSE_DETECTED = "reuse_detected"
    PASSWORD_CHANGED = "password_changed"
    ADMIN = "admin"


class TokenError(Exception):
    """Base for every token failure. Never surfaced to clients verbatim."""


class TokenExpiredError(TokenError):
    pass


class TokenInvalidError(TokenError):
    pass


class TokenReuseError(TokenError):
    """A used refresh token was presented again: treat as compromise."""


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # a scheme name, not a credential
    expires_in: int = 0


@dataclass(frozen=True, slots=True)
class PurposeClaims:
    subject: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime
    token_type: TokenType


def _now() -> datetime:
    """Always timezone-aware. Naive datetimes silently compare wrong across
    DST and UTC boundaries, which in this module means tokens that outlive
    their expiry."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Coerce a value read back from the database to an aware UTC datetime.

    Postgres `TIMESTAMPTZ` round-trips the offset, but SQLite has no timezone
    type and hands back a naive value. Comparing that against an aware `now()`
    raises `TypeError`, so an expiry check would crash instead of expiring the
    token. Everything is stored as UTC, so attaching UTC is the correct reading.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def audience_for(token_type: TokenType) -> str:
    """Every purpose gets a distinct audience.

    This is the mechanism that stops a low-authority token being replayed as a
    high-authority one — see the note on :class:`TokenType`.
    """
    return f"{get_settings().jwt_audience}:{token_type.value}"


# ---------------------------------------------------------------------------
# Purpose-bound tokens (JWT)
# ---------------------------------------------------------------------------


def create_purpose_token(
    user_id: uuid.UUID,
    *,
    token_type: TokenType,
    ttl_seconds: int,
    extra_claims: dict[str, str | bool | int] | None = None,
) -> tuple[str, PurposeClaims]:
    settings = get_settings()
    issued_at = _now()
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    jti = str(uuid.uuid4())

    payload: dict[str, object] = {
        "sub": str(user_id),
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        # `iss` and `aud` are validated on the way back in. Without them, a token
        # minted by a sibling service that happens to share a secret would verify.
        "iss": settings.jwt_issuer,
        "aud": audience_for(token_type),
        "typ": token_type.value,
    }
    if extra_claims:
        payload |= extra_claims

    encoded = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    claims = PurposeClaims(
        subject=user_id,
        jti=jti,
        issued_at=issued_at,
        expires_at=expires_at,
        token_type=token_type,
    )
    return encoded, claims


def decode_purpose_token(token: str, *, expected_type: TokenType) -> PurposeClaims:
    """Decode and validate a purpose-bound token.

    ``expected_type`` is mandatory and drives the audience check, so a caller
    cannot accidentally accept "any valid token we ever issued".
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Pinning the algorithm list is what prevents the `alg: none` and
            # RS256->HS256 confusion attacks. Never pass the token's own header.
            algorithms=[settings.jwt_algorithm],
            audience=audience_for(expected_type),
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError("Token is invalid.") from exc

    # Belt and braces: the audience check above already enforces this, but an
    # explicit claim check keeps the invariant true if audiences are ever
    # reconfigured to collide.
    actual_type = payload.get("typ")
    if actual_type != expected_type.value:
        raise TokenInvalidError(f"Expected a {expected_type.value} token, got {actual_type!r}.")

    try:
        subject = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenInvalidError("Token subject is not a valid user id.") from exc

    return PurposeClaims(
        subject=subject,
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        token_type=expected_type,
    )


# ---------------------------------------------------------------------------
# Refresh tokens (opaque, rotating)
# ---------------------------------------------------------------------------


async def issue_refresh_token(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> str:
    """Mint a refresh token. Returns the plaintext, which is never persisted."""
    settings = get_settings()
    token = random_token()

    record = RefreshToken(
        user_id=user.id,
        token_hash=sha256_hex(token),
        family_id=family_id or uuid.uuid4(),
        expires_at=_now() + timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=(user_agent or "")[:512] or None,
        ip_address=(ip_address or "")[:45] or None,
    )
    session.add(record)
    await session.flush()
    return token


async def revoke_family(
    session: AsyncSession,
    family_id: uuid.UUID,
    *,
    reason: RevocationReason,
) -> int:
    """Revoke every live token descended from one login. Returns the count."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason.value)
    )
    # `execute` is typed as returning Result; an UPDATE always yields a
    # CursorResult, which is the only thing that carries rowcount.
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def revoke_by_token(
    session: AsyncSession,
    presented: str,
    *,
    reason: RevocationReason,
) -> bool:
    """Revoke one specific refresh token. Returns whether a live row was found.

    Used by logout: clearing the cookie only removes the browser's copy, so a
    token captured in transit or from a log would stay rotatable without this.
    """
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == sha256_hex(presented),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now(), revoked_reason=reason.value)
    )
    return bool(cast("CursorResult[Any]", result).rowcount)


async def revoke_all_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    reason: RevocationReason,
) -> int:
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason.value)
    )
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def rotate_refresh_token(
    session: AsyncSession,
    presented: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[User, str]:
    """Exchange a refresh token for a fresh one.

    Raises :class:`TokenReuseError` when the presented token has already been
    rotated — the whole family is revoked before the exception propagates.
    """
    token_hash = sha256_hex(presented)

    # Row lock: two concurrent refreshes with the same token must not both
    # succeed. Whichever transaction commits second sees used_at set and is
    # correctly treated as reuse.
    statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    statement = statement.with_for_update()
    record = (await session.execute(statement)).scalar_one_or_none()

    if record is None:
        raise TokenInvalidError("Unknown refresh token.")

    # Order matters: `used_at` is checked BEFORE `revoked_at`.
    #
    # Rotation marks a token both used and revoked. If the revoked branch ran
    # first, every replay of a normally-rotated token would be reported as a
    # plain "revoked" error and the family would never be torn down — silently
    # disabling reuse detection entirely, which is the whole point of rotation.
    if record.used_at is not None:
        # The defining case. A token is single-use; a second presentation means
        # two parties hold it, and we cannot tell which one is the attacker.
        # Revoking the family logs out both, forcing a fresh authentication.
        revoked = await revoke_family(
            session, record.family_id, reason=RevocationReason.REUSE_DETECTED
        )
        await session.commit()
        logger.warning(
            "refresh_token_reuse_detected",
            user_id=str(record.user_id),
            family_id=str(record.family_id),
            revoked_tokens=revoked,
            ip_address=ip_address,
        )
        raise TokenReuseError("Refresh token reuse detected; all sessions revoked.")

    if record.revoked_at is not None:
        # Revoked without ever being used: logout, password change or an admin
        # action. Not evidence of theft, so no family teardown.
        raise TokenInvalidError("Refresh token has been revoked.")

    if _as_utc(record.expires_at) <= _now():
        raise TokenExpiredError("Refresh token has expired.")

    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise TokenInvalidError("User is not active.")

    record.used_at = _now()
    record.revoked_at = _now()
    record.revoked_reason = RevocationReason.ROTATED.value

    replacement = await issue_refresh_token(
        session,
        user,
        family_id=record.family_id,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    return user, replacement


async def prune_expired(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """Delete rows well past expiry.

    Retention beyond expiry is intentional: a revoked-and-expired row is still
    the evidence that reuse detection fired. Run this from a scheduled job.
    """
    cutoff = _now() - timedelta(days=older_than_days)
    result = await session.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff))
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def issued_before_password_change(issued_at: datetime, user: User) -> bool:
    """True when a token predates the user's last password change.

    Access tokens are stateless, so a password change cannot retract the ones
    already out there. Checking this at request time closes the window without
    giving up stateless verification. Wired into the request path by
    ``app.auth.engine.dependencies``.
    """
    if user.password_changed_at is None:
        return False
    return _as_utc(issued_at) < _as_utc(user.password_changed_at)
