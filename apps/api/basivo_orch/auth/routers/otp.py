"""Passwordless login and step-up authentication with one-time codes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.email.sender import send_otp_email
from basivo_orch.auth.engine import UserManager, get_user_manager
from basivo_orch.auth.engine.types import UserNotExists
from basivo_orch.auth.routers.session import issue_session
from basivo_orch.auth.schemas import MessageResponse, OTPRequestPayload, OTPVerifyPayload, TokenResponse
from basivo_orch.auth.security import otp, redis_client, tokens
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.ratelimit import client_ip, limiter
from basivo_orch.auth.settings import get_settings

router = APIRouter(prefix="/auth/otp", tags=["otp"])

GENERIC_SEND_MESSAGE = "If an account exists for that address, a code has been sent."

RESEND_COOLDOWN_SECONDS = 60
"""Minimum gap between sends. Stops the endpoint being used to flood an inbox
(or run up an SMS bill) for an address the caller does not control."""


@router.post("/request", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(get_settings().otp_send_rate_limit)
async def request_code(
    request: Request,
    response: Response,
    payload: OTPRequestPayload,
    manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Send a login code.

    Same response for every outcome — registered, unknown, inactive, or on
    cooldown — so this cannot be used to test which addresses have accounts.
    """
    settings = get_settings()
    state = redis_client.get_redis()

    try:
        user = await manager.get_by_email(payload.email)
    except UserNotExists:
        await record(
            session,
            action=AuditAction.OTP_SENT,
            outcome=Outcome.FAILURE,
            ip_address=client_ip(request),
            detail={"reason": "unknown_account"},
            commit=True,
        )
        return MessageResponse(detail=GENERIC_SEND_MESSAGE)

    if not user.is_active:
        return MessageResponse(detail=GENERIC_SEND_MESSAGE)

    remaining = await otp.peek_ttl(state, identifier=payload.email, purpose=otp.OTPPurpose.LOGIN)
    if remaining > settings.otp_ttl_seconds - RESEND_COOLDOWN_SECONDS:
        return MessageResponse(detail=GENERIC_SEND_MESSAGE)

    issued = await otp.issue(state, identifier=payload.email, purpose=otp.OTPPurpose.LOGIN)
    await send_otp_email(user.email, issued.code, purpose="sign in")

    await record(
        session,
        action=AuditAction.OTP_SENT,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
        commit=True,
    )
    return MessageResponse(detail=GENERIC_SEND_MESSAGE)


@router.post("/verify", response_model=TokenResponse)
@limiter.limit("10/15minutes")
async def verify_code(
    request: Request,
    response: Response,
    payload: OTPVerifyPayload,
    manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    """Exchange a valid code for a session."""
    state = redis_client.get_redis()

    result = await otp.verify(
        state,
        identifier=payload.email,
        purpose=otp.OTPPurpose.LOGIN,
        code=payload.code,
    )

    if result is not otp.OTPResult.VALID:
        await record(
            session,
            action=AuditAction.OTP_VERIFIED,
            outcome=Outcome.FAILURE,
            ip_address=client_ip(request),
            detail={"result": result.value},
            commit=True,
        )
        # One message for invalid, expired and exhausted alike: distinguishing
        # them tells an attacker whether to keep guessing this code or start over.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is not valid. Request a new one.",
        )

    try:
        user = await manager.get_by_email(payload.email)
    except UserNotExists as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is not valid. Request a new one.",
        ) from exc

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled."
        )

    # Possession of the emailed code proves control of the address, so this is
    # also a valid email verification.
    if not user.is_verified:
        await manager.user_db.update(user, {"is_verified": True})
    if user.totp_enabled:
        # First factor satisfied, second still outstanding. Issue a step-up
        # token, which is bound to its own audience and cannot authenticate
        # any API route.
        step_up_token, _ = tokens.create_purpose_token(
            user.id,
            token_type=tokens.TokenType.STEP_UP,
            ttl_seconds=300,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Two-factor authentication required.",
            headers={"X-Step-Up-Token": step_up_token, "X-Step-Up-Methods": "totp"},
        )

    session_payload = await issue_session(session, user, request, response)
    await manager.on_after_login(user, request, response)
    await record(
        session,
        action=AuditAction.OTP_VERIFIED,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()
    return session_payload
