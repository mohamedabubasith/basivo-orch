"""Password lifecycle: forgot, reset, change.

These endpoints wrap the engine's manager rather than mounting its stock
reset-password router, so that rate limiting, audit logging and the
enumeration-safe response shape are all under this service's control.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.email.sender import send_password_changed_email
from basivo_orch.auth.engine import UserManager, current_active_user, get_user_manager
from basivo_orch.auth.engine.types import (
    InvalidPasswordException,
    InvalidResetPasswordToken,
    UserNotExists,
)
from basivo_orch.auth.models import User
from basivo_orch.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    MessageResponse,
    ResetPasswordRequest,
)
from basivo_orch.auth.security import tokens
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.passwords import dummy_verify, validate
from basivo_orch.auth.security.ratelimit import client_ip, limiter
from basivo_orch.auth.settings import get_settings

router = APIRouter(prefix="/auth", tags=["password"])

# One message for every outcome. If a registered address produced "check your
# inbox" and an unknown one produced "no such user", this endpoint would be a
# free user-directory lookup for anyone.
GENERIC_RESET_MESSAGE = (
    "If an account exists for that address, a password reset link has been sent."
)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(get_settings().forgot_password_rate_limit)
async def forgot_password(
    request: Request,
    response: Response,
    payload: ForgotPasswordRequest,
    manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Start a password reset.

    Always returns 202 with the same body. The unknown-address branch still
    performs a dummy hash so the two paths take comparable wall-clock time —
    a response-time difference is just as good an oracle as a different message.
    """
    try:
        user = await manager.get_by_email(payload.email)
    except UserNotExists:
        dummy_verify()
        await record(
            session,
            action=AuditAction.PASSWORD_RESET_REQUEST,
            outcome=Outcome.FAILURE,
            ip_address=client_ip(request),
            detail={"reason": "unknown_account"},
            commit=True,
        )
        return MessageResponse(detail=GENERIC_RESET_MESSAGE)

    if user.is_active:
        # on_after_forgot_password sends the email and writes the audit row.
        await manager.forgot_password(user, request)
        await session.commit()

    return MessageResponse(detail=GENERIC_RESET_MESSAGE)


@router.post("/reset-password", response_model=MessageResponse)
@limiter.limit("10/hour")
async def reset_password(
    request: Request,
    response: Response,
    payload: ResetPasswordRequest,
    manager: UserManager = Depends(get_user_manager),
) -> MessageResponse:
    """Complete a password reset.

    ``on_after_reset_password`` stamps ``password_changed_at`` and revokes every
    refresh token, so a reset genuinely ends all sessions — including whichever
    one an attacker may hold.
    """
    try:
        await manager.reset_password(payload.token, payload.new_password, request)
    except (InvalidResetPasswordToken, UserNotExists) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired. Request a new one.",
        ) from exc
    except InvalidPasswordException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.reason,
        ) from exc

    return MessageResponse(detail="Your password has been reset. Please sign in.")


@router.post("/change-password", response_model=MessageResponse)
@limiter.limit("10/hour")
async def change_password(
    request: Request,
    response: Response,
    payload: ChangePasswordRequest,
    user: User = Depends(current_active_user),
    manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Change the password of the signed-in user.

    Requires the current password even though the caller is already
    authenticated: it re-proves possession of the credential, so a hijacked
    session alone cannot lock the real owner out of their account.
    """
    verified, _ = manager.password_helper.verify_and_update(
        payload.current_password, user.hashed_password
    )
    if not verified:
        await record(
            session,
            action=AuditAction.PASSWORD_CHANGE,
            outcome=Outcome.FAILURE,
            user_id=user.id,
            ip_address=client_ip(request),
            detail={"reason": "current_password_mismatch"},
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    result = await validate(payload.new_password, email=user.email)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=" ".join(result.errors),
        )

    from datetime import UTC, datetime

    await manager.user_db.update(
        user,
        {
            "hashed_password": manager.password_helper.hash(payload.new_password),
            "password_changed_at": datetime.now(UTC),
        },
    )

    revoked = await tokens.revoke_all_for_user(
        session, user.id, reason=tokens.RevocationReason.PASSWORD_CHANGED
    )
    await record(
        session,
        action=AuditAction.PASSWORD_CHANGE,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
        detail={"revoked_sessions": revoked},
    )
    await session.commit()

    await send_password_changed_email(user.email)
    return MessageResponse(detail="Password changed. All other sessions have been signed out.")
