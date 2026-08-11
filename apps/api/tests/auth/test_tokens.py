"""Refresh token rotation and reuse detection.

These are the assertions that make the reuse-detection claim real rather than
aspirational. If any of them fail, a stolen refresh token grants indefinite
access and nobody finds out.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from basivo_orch.auth.models import RefreshToken, User
from basivo_orch.auth.security import tokens

pytestmark = pytest.mark.security


async def test_rotation_issues_a_new_token_and_burns_the_old(session, user: User) -> None:
    original = await tokens.issue_refresh_token(session, user)
    await session.commit()

    rotated_user, replacement = await tokens.rotate_refresh_token(session, original)
    await session.commit()

    assert rotated_user.id == user.id
    assert replacement != original

    old = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == tokens.sha256_hex(original))
        )
    ).scalar_one()
    assert old.used_at is not None
    assert old.revoked_at is not None
    assert old.revoked_reason == tokens.RevocationReason.ROTATED.value


async def test_rotation_keeps_the_family(session, user: User) -> None:
    """Descendants share a family so theft can revoke the whole chain."""
    first = await tokens.issue_refresh_token(session, user)
    await session.commit()
    _, second = await tokens.rotate_refresh_token(session, first)
    await session.commit()

    rows = (
        (await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len({row.family_id for row in rows}) == 1
    assert len(rows) == 2
    assert second


async def test_reusing_a_rotated_token_revokes_the_entire_family(session, user: User) -> None:
    """The core anti-theft property.

    Scenario: an attacker exfiltrates the refresh token and uses it. The
    legitimate client still holds the same value and refreshes later. That
    second presentation is impossible unless the token leaked, so everything
    descended from that login is revoked.
    """
    stolen = await tokens.issue_refresh_token(session, user)
    await session.commit()

    # Attacker rotates first and gets a working token.
    _, attacker_token = await tokens.rotate_refresh_token(session, stolen)
    await session.commit()

    # Victim's client presents the value it still holds.
    with pytest.raises(tokens.TokenReuseError):
        await tokens.rotate_refresh_token(session, stolen)

    rows = (
        (await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id)))
        .scalars()
        .all()
    )
    assert all(row.revoked_at is not None for row in rows), "every token in the family must be dead"
    assert any(row.revoked_reason == tokens.RevocationReason.REUSE_DETECTED.value for row in rows)

    # And the attacker's freshly minted token is now useless too.
    with pytest.raises(tokens.TokenInvalidError):
        await tokens.rotate_refresh_token(session, attacker_token)


async def test_expired_refresh_token_is_rejected(session, user: User) -> None:
    token = await tokens.issue_refresh_token(session, user)
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == tokens.sha256_hex(token))
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(tokens.TokenExpiredError):
        await tokens.rotate_refresh_token(session, token)


async def test_unknown_refresh_token_is_rejected(session) -> None:
    with pytest.raises(tokens.TokenInvalidError):
        await tokens.rotate_refresh_token(session, "not-a-real-token")


async def test_plaintext_refresh_token_is_never_stored(session, user: User) -> None:
    token = await tokens.issue_refresh_token(session, user)
    await session.commit()

    rows = (await session.execute(select(RefreshToken))).scalars().all()
    stored = {row.token_hash for row in rows}
    assert token not in stored
    assert tokens.sha256_hex(token) in stored


async def test_revoking_for_user_kills_every_live_session(session, user: User) -> None:
    for _ in range(3):
        await tokens.issue_refresh_token(session, user)
    await session.commit()

    revoked = await tokens.revoke_all_for_user(
        session, user.id, reason=tokens.RevocationReason.PASSWORD_CHANGED
    )
    await session.commit()
    assert revoked == 3

    rows = (await session.execute(select(RefreshToken))).scalars().all()
    assert all(row.revoked_at is not None for row in rows)


# ---------------------------------------------------------------------------
# Purpose-bound tokens
# ---------------------------------------------------------------------------


def test_purpose_tokens_do_not_cross_validate() -> None:
    """A token minted for one purpose must not validate for another.

    Both are signed with the same derived JWT key, so only the audience
    separates them. If
    this regressed, a low-authority token (a pending-2FA step-up, say) could be
    replayed where a high-authority one is expected.
    """
    subject = uuid.uuid4()
    token, _ = tokens.create_purpose_token(
        subject, token_type=tokens.TokenType.RESET_PASSWORD, ttl_seconds=600
    )

    claims = tokens.decode_purpose_token(token, expected_type=tokens.TokenType.RESET_PASSWORD)
    assert claims.subject == subject

    with pytest.raises(tokens.TokenInvalidError):
        tokens.decode_purpose_token(token, expected_type=tokens.TokenType.VERIFY_EMAIL)


def test_purpose_token_is_not_accepted_by_the_access_token_strategy() -> None:
    """The engine's access-token audience must reject purpose tokens outright."""
    import jwt

    from basivo_orch.auth.settings import get_settings

    settings = get_settings()
    token, _ = tokens.create_purpose_token(
        uuid.uuid4(), token_type=tokens.TokenType.RESET_PASSWORD, ttl_seconds=600
    )

    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(
            token,
            settings.subkey_str("jwt"),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )


def test_expired_purpose_token_is_rejected() -> None:
    token, _ = tokens.create_purpose_token(
        uuid.uuid4(), token_type=tokens.TokenType.RESET_PASSWORD, ttl_seconds=-1
    )
    with pytest.raises(tokens.TokenExpiredError):
        tokens.decode_purpose_token(token, expected_type=tokens.TokenType.RESET_PASSWORD)


def test_token_signed_with_a_different_secret_is_rejected() -> None:
    import jwt

    from basivo_orch.auth.settings import get_settings

    settings = get_settings()
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": settings.jwt_issuer,
            "aud": tokens.audience_for(tokens.TokenType.RESET_PASSWORD),
            "typ": tokens.TokenType.RESET_PASSWORD.value,
        },
        # Long enough to clear PyJWT's minimum-key-length warning, so the test
        # asserts signature rejection rather than incidentally tripping a
        # different guard.
        "an-attacker-chosen-secret-of-entirely-sufficient-length",
        algorithm="HS256",
    )
    with pytest.raises(tokens.TokenInvalidError):
        tokens.decode_purpose_token(forged, expected_type=tokens.TokenType.RESET_PASSWORD)


def test_alg_none_token_is_rejected() -> None:
    """The classic JWT bypass: an unsigned token claiming alg=none."""
    import jwt

    from basivo_orch.auth.settings import get_settings

    settings = get_settings()
    unsigned = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "iss": settings.jwt_issuer,
            "aud": tokens.audience_for(tokens.TokenType.RESET_PASSWORD),
            "typ": tokens.TokenType.RESET_PASSWORD.value,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(tokens.TokenInvalidError):
        tokens.decode_purpose_token(unsigned, expected_type=tokens.TokenType.RESET_PASSWORD)
