"""Login, logout, and refresh-token rotation.

The engine ships an auth router, but this service deliberately does **not** mount
its login route. Three things it cannot do, all of which matter:

* **Issue a refresh token.** The engine has no concept of one, so a login through
  its router produces an access token and nothing else — leaving the rotation and
  reuse-detection machinery in ``app.auth.security.tokens`` unreachable from the
  primary sign-in path.
* **Enforce rate limiting.** ``LOGIN_RATE_LIMIT`` can only be applied to a route
  this service owns.
* **Enforce the second factor.** The engine knows nothing about ``totp_enabled``,
  so its login route hands out a full session to an account with 2FA switched on
  — bypassing it entirely.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.engine import (
    UserManager,
    current_active_user,
    get_jwt_strategy,
    get_user_manager,
)
from basivo_orch.auth.models import User
from basivo_orch.auth.schemas import MessageResponse, RefreshRequest, TokenResponse
from basivo_orch.auth.security import tokens
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.headers import CSRF_HEADER_NAME, set_csrf_cookie
from basivo_orch.auth.security.ratelimit import client_ip, limiter
from basivo_orch.auth.settings import get_settings

router = APIRouter(prefix="/auth", tags=["session"])

REFRESH_COOKIE_PATH = "/auth"
"""Scoping the cookie to /auth means it is not attached to ordinary API calls,
so the long-lived credential is exposed on far fewer requests."""


def set_access_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.access_token_ttl_seconds,
        # The point of the cookie transport: script on the page cannot read it,
        # which removes token exfiltration via XSS.
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        domain=settings.cookie_domain,
        path="/",
    )


def clear_access_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.cookie_name,
        domain=settings.cookie_domain,
        path="/",
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        domain=settings.cookie_domain,
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        domain=settings.cookie_domain,
        path=REFRESH_COOKIE_PATH,
    )


def _extract_refresh_token(request: Request, payload: RefreshRequest | None) -> str:
    settings = get_settings()
    from_cookie = request.cookies.get(settings.refresh_cookie_name)
    if from_cookie:
        return from_cookie
    if payload and payload.refresh_token:
        return payload.refresh_token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No refresh token supplied.",
    )


async def issue_session(
    session: AsyncSession,
    user: User,
    request: Request,
    response: Response,
) -> TokenResponse:
    """Mint an access + refresh pair and attach it to the response.

    Shared by every successful sign-in path — password, OTP, magic link and the
    2FA exchange — so the token shape and cookie flags cannot drift between them.
    """
    settings = get_settings()

    refresh_token = await tokens.issue_refresh_token(
        session,
        user,
        user_agent=request.headers.get("user-agent"),
        ip_address=client_ip(request),
    )
    access_token = await get_jwt_strategy().write_token(user)
    set_access_cookie(response, access_token)
    set_refresh_cookie(response, refresh_token)
    # Hand the client its CSRF token here. Without this nothing could ever
    # satisfy CSRFMiddleware, and every cookie-authenticated mutating request
    # would be permanently rejected.
    response.headers[CSRF_HEADER_NAME] = set_csrf_cookie(response)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.access_token_ttl_seconds,
    )


@router.get("/csrf", response_model=MessageResponse)
async def csrf(response: Response) -> MessageResponse:
    """Mint a CSRF token without authenticating.

    A GET is safe to expose: the token's value is not a secret. Its protection
    comes from the same-origin policy — a cross-site attacker can make the
    browser send our cookie but cannot read our response to learn what to put
    in the header.

    Needed so a page reload, or a client that never went through a sign-in on
    this device, can still make mutating requests.
    """
    token = set_csrf_cookie(response)
    response.headers[CSRF_HEADER_NAME] = token
    return MessageResponse(detail=token)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(get_settings().login_rate_limit)
async def login(
    request: Request,
    response: Response,
    credentials: OAuth2PasswordRequestForm = Depends(),
    manager: UserManager = Depends(get_user_manager),
    session: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    """Exchange credentials for a session.

    ``manager.authenticate`` applies lockout and equalises response timing
    between "wrong password" and "no such account"; see
    ``app.auth.engine.manager``.
    """
    user = await manager.authenticate(credentials)

    if user is None or not user.is_active:
        await record(
            session,
            action=AuditAction.LOGIN,
            outcome=Outcome.FAILURE,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            commit=True,
        )
        # One message for both branches. A distinguishable response here is a
        # membership oracle for every address an attacker cares to test.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LOGIN_BAD_CREDENTIALS",
        )

    if user.totp_enabled:
        # First factor satisfied, second outstanding. The step-up token is bound
        # to its own audience and carries no API authority — it can only be
        # exchanged at /auth/2fa/verify.
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

    payload = await issue_session(session, user, request, response)
    await manager.on_after_login(user, request, response)
    await session.commit()
    return payload


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """End the current session.

    Revokes the presented refresh token so it cannot be rotated after logout —
    clearing the cookie alone would leave a captured token usable.
    """
    presented = request.cookies.get(get_settings().refresh_cookie_name)
    if presented is None and payload is not None:
        presented = payload.refresh_token

    if presented:
        await tokens.revoke_by_token(session, presented, reason=tokens.RevocationReason.LOGOUT)

    await record(
        session,
        action=AuditAction.LOGOUT,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()
    clear_access_cookie(response)
    clear_refresh_cookie(response)
    return MessageResponse(detail="Signed out.")


@router.post("/refresh", response_model=TokenResponse)
# Per client IP. Every open tab refreshes once per access-token lifetime, and
# an office behind one NAT address is many clients; 30/minute signed a whole
# team out whenever a few of them were working. Reuse detection, not this
# limit, is what stops a stolen token.
@limiter.limit("120/minute")
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    session: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    """Exchange a refresh token for a new access token and a new refresh token.

    The old refresh token is invalidated. Presenting it again is treated as
    theft: the entire token family is revoked, which signs out both the attacker
    and the legitimate user, and forces a fresh authentication.
    """
    settings = get_settings()
    presented = _extract_refresh_token(request, payload)

    try:
        user, replacement = await tokens.rotate_refresh_token(
            session,
            presented,
            user_agent=request.headers.get("user-agent"),
            ip_address=client_ip(request),
        )
    except tokens.TokenReuseError as exc:
        await record(
            session,
            action=AuditAction.TOKEN_REUSE_DETECTED,
            outcome=Outcome.BLOCKED,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            commit=True,
        )
        clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            # Deliberately vague. Telling an attacker that reuse detection fired
            # tells them the victim is active and the theft was noticed.
            detail="Session is no longer valid. Please sign in again.",
        ) from exc
    except (tokens.TokenExpiredError, tokens.TokenInvalidError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer valid. Please sign in again.",
        ) from exc

    strategy = get_jwt_strategy()
    access_token = await strategy.write_token(user)

    await record(
        session,
        action=AuditAction.TOKEN_REFRESH,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    await session.commit()
    set_refresh_cookie(response, replacement)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.access_token_ttl_seconds,
    )


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    request: Request,
    response: Response,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Revoke every refresh token for the current user.

    Distinct from the engine's ``/auth/logout``, which only clears the current
    transport. This is the "sign out everywhere" control a user reaches for
    after losing a device.
    """
    revoked = await tokens.revoke_all_for_user(
        session, user.id, reason=tokens.RevocationReason.LOGOUT
    )
    await record(
        session,
        action=AuditAction.LOGOUT,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
        detail={"revoked_sessions": revoked},
    )
    await session.commit()
    clear_refresh_cookie(response)
    return MessageResponse(detail=f"Signed out of {revoked} session(s).")
