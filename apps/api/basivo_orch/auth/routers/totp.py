"""TOTP two-factor enrolment and verification."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.engine import current_active_user, current_fresh_user
from basivo_orch.auth.models import RecoveryCode, User
from basivo_orch.auth.routers.session import issue_session
from basivo_orch.auth.schemas import (
    MessageResponse,
    TokenResponse,
    TOTPConfirmPayload,
    TOTPEnrolComplete,
    TOTPEnrolStart,
    TOTPVerifyPayload,
)
from basivo_orch.auth.security import tokens, totp
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.ratelimit import client_ip, limiter

router = APIRouter(prefix="/auth/2fa", tags=["two-factor"])


@router.post("/enrol", response_model=TOTPEnrolStart)
@limiter.limit("5/hour")
async def start_enrolment(
    request: Request,
    response: Response,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> TOTPEnrolStart:
    """Begin enrolment.

    The seed is stored encrypted but ``totp_enabled`` stays false until a code
    is confirmed. Enabling on issue would lock out any user whose authenticator
    failed to scan the QR.
    """
    if user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-factor authentication is already enabled.",
        )

    enrolment = totp.start_enrolment(user.email)
    user.totp_secret = enrolment.encrypted_secret
    user.totp_enabled = False
    user.totp_last_counter = None
    await session.commit()

    return TOTPEnrolStart(
        # Shown once, so a user with a camera-less device can type it in.
        secret=enrolment.secret,
        provisioning_uri=enrolment.provisioning_uri,
        qr_code_svg=totp.render_qr_svg(enrolment.provisioning_uri),
    )


@router.post("/enrol/confirm", response_model=TOTPEnrolComplete)
@limiter.limit("10/hour")
async def confirm_enrolment(
    request: Request,
    response: Response,
    payload: TOTPConfirmPayload,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> TOTPEnrolComplete:
    """Confirm enrolment with a live code and issue recovery codes."""
    if user.totp_secret is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start enrolment first.",
        )
    if user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-factor authentication is already enabled.",
        )

    valid, counter = totp.verify_code(user.totp_secret, payload.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is not valid. Check your authenticator app's clock.",
        )

    user.totp_enabled = True
    user.totp_confirmed_at = datetime.now(UTC)
    user.totp_last_counter = counter

    # Replace any previous set: leaving old codes live would mean a user who
    # re-enrols after losing their phone still has codes they cannot see.
    existing = (
        await session.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id))
    ).scalars()
    for row in existing:
        await session.delete(row)

    codes = totp.generate_recovery_codes()
    for code in codes:
        session.add(
            RecoveryCode(
                user_id=user.id,
                code_hash=totp.hash_recovery_code(code),
                created_at=datetime.now(UTC),
            )
        )

    await record(
        session,
        action=AuditAction.TOTP_ENROLLED,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()

    # The only time these are ever readable. Only hashes were persisted.
    return TOTPEnrolComplete(recovery_codes=codes)


@router.post("/verify", response_model=TokenResponse)
@limiter.limit("10/15minutes")
async def verify(
    request: Request,
    response: Response,
    payload: TOTPVerifyPayload,
    session: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    """Complete a two-factor login.

    Accepts either a TOTP code or a recovery code, in exchange for the step-up
    token issued when the first factor succeeded.
    """
    try:
        claims = tokens.decode_purpose_token(
            payload.step_up_token, expected_type=tokens.TokenType.STEP_UP
        )
    except tokens.TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This sign-in attempt has expired. Start again.",
        ) from exc

    user = await session.get(User, claims.subject)
    if user is None or not user.is_active or not user.totp_enabled or user.totp_secret is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This sign-in attempt has expired. Start again.",
        )

    valid, counter = totp.verify_code(
        user.totp_secret, payload.code, last_counter=user.totp_last_counter
    )
    used_recovery = False

    if valid:
        user.totp_last_counter = counter
    else:
        used_recovery = await _consume_recovery_code(session, user, payload.code)
        if not used_recovery:
            await record(
                session,
                action=AuditAction.TOTP_VERIFIED,
                outcome=Outcome.FAILURE,
                user_id=user.id,
                ip_address=client_ip(request),
                commit=True,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That code is not valid.",
            )

    session_payload = await issue_session(session, user, request, response)
    user.last_login_at = datetime.now(UTC)
    user.last_login_ip = client_ip(request)

    await record(
        session,
        action=AuditAction.RECOVERY_CODE_USED if used_recovery else AuditAction.TOTP_VERIFIED,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()
    return session_payload


async def _consume_recovery_code(session: AsyncSession, user: User, submitted: str) -> bool:
    """Spend a recovery code if it matches an unused one.

    Looked up by hash, so a code is never compared in plaintext, and marked used
    rather than deleted so the audit trail survives.
    """
    candidate_hash = totp.hash_recovery_code(submitted)
    statement = select(RecoveryCode).where(
        RecoveryCode.user_id == user.id,
        RecoveryCode.code_hash == candidate_hash,
        RecoveryCode.used_at.is_(None),
    )
    statement = statement.with_for_update()
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        return False

    row.used_at = datetime.now(UTC)
    return True


@router.post("/disable", response_model=MessageResponse)
async def disable(
    request: Request,
    user: User = Depends(current_fresh_user),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Turn off two-factor authentication.

    Gated on ``current_fresh_user``: removing a second factor is exactly what an
    attacker holding a stolen session would do first, so it requires a recent
    authentication rather than merely a valid one.
    """
    if not user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-factor authentication is not enabled.",
        )

    user.totp_enabled = False
    user.totp_secret = None
    user.totp_confirmed_at = None
    user.totp_last_counter = None

    existing = (
        await session.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id))
    ).scalars()
    for row in existing:
        await session.delete(row)

    await record(
        session,
        action=AuditAction.TOTP_DISABLED,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()

    return MessageResponse(detail="Two-factor authentication disabled.")
