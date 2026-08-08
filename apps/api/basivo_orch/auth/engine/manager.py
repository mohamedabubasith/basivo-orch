"""User lifecycle manager.

Subclasses ``fastapi_users.BaseUserManager`` to attach this service's policy:
password validation, lockout, audit logging and transactional email. All of it
is expressed as hook overrides, so upgrading the engine underneath keeps the
policy intact.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import structlog
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, InvalidPasswordException, UUIDIDMixin, schemas
from fastapi_users.password import PasswordHelper

from basivo_orch.auth.email.sender import (
    send_password_changed_email,
    send_reset_password_email,
    send_verify_email,
)
from basivo_orch.auth.engine.adapters import UserDatabase, get_user_db
from basivo_orch.auth.models import User
from basivo_orch.auth.security import lockout, redis_client
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.passwords import dummy_verify, password_hash, validate
from basivo_orch.auth.security.ratelimit import client_ip
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)

# Hand our Argon2id-configured hasher to the engine so there is exactly one
# password hashing configuration in the process.
password_helper = PasswordHelper(password_hash)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """Policy layer over the user store."""

    def __init__(
        self,
        user_db: UserDatabase,
        password_helper: PasswordHelper | None = None,
    ) -> None:
        super().__init__(user_db, password_helper)
        # Set here rather than as class attributes so the values come from
        # Settings at request time, which is what lets tests override them —
        # and rather than as properties, because the base class declares them
        # as writeable attributes.
        settings = get_settings()
        self.reset_password_token_secret = settings.secret_key.get_secret_value()
        self.verification_token_secret = settings.secret_key.get_secret_value()
        self.reset_password_token_lifetime_seconds = settings.reset_password_token_ttl_seconds
        self.verification_token_lifetime_seconds = settings.verify_email_token_ttl_seconds

    # -- Validation --------------------------------------------------------

    async def validate_password(self, password: str, user: schemas.UC | User) -> None:
        result = await validate(password, email=getattr(user, "email", None))
        if not result.ok:
            raise InvalidPasswordException(reason=" ".join(result.errors))

    # -- Authentication ----------------------------------------------------

    async def authenticate(self, credentials: OAuth2PasswordRequestForm) -> User | None:
        """Verify credentials with lockout and constant-time behaviour.

        Two properties this override exists to guarantee:

        1. A locked-out account is rejected before any hashing happens.
        2. A non-existent account costs the same wall-clock time as a real one
           with a wrong password. Without the dummy verification below, the
           missing-user branch returns in microseconds while the real branch
           spends ~50ms in Argon2 — a timing oracle that enumerates every
           registered address.
        """
        email = credentials.username.strip().lower()
        store = redis_client.get_redis()

        state = await lockout.check(store, identifier=email)
        if state.locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts. Try again later.",
                headers={"Retry-After": str(state.retry_after_seconds)},
            )

        user = await self.user_db.get_by_email(email)
        if user is None:
            dummy_verify()
            await lockout.record_failure(store, identifier=email)
            return None

        verified, updated_hash = self.password_helper.verify_and_update(
            credentials.password, user.hashed_password
        )
        if not verified:
            await lockout.record_failure(store, identifier=email)
            return None

        if not user.is_active:
            # Counted as a failure so a disabled account cannot be used as an
            # unlimited oracle for testing whether a password is correct.
            await lockout.record_failure(store, identifier=email)
            return None

        if updated_hash is not None:
            # The stored hash used weaker parameters. This is the only moment we
            # legitimately hold the plaintext, so upgrade it now.
            await self.user_db.update(user, {"hashed_password": updated_hash})

        await lockout.reset(store, identifier=email)
        return user

    # -- Lifecycle hooks ---------------------------------------------------

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        logger.info("user_registered", user_id=str(user.id))
        await record(
            self.user_db.session,  # type: ignore[attr-defined]
            action=AuditAction.REGISTER,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
            ip_address=client_ip(request) if request else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
        if not user.is_verified:
            await self.request_verify(user, request)

    async def on_after_login(
        self,
        user: User,
        request: Request | None = None,
        response: Response | None = None,
    ) -> None:
        await self.user_db.update(
            user,
            {
                "last_login_at": datetime.now(UTC),
                "last_login_ip": client_ip(request) if request else None,
                "failed_login_count": 0,
            },
        )
        await record(
            self.user_db.session,  # type: ignore[attr-defined]
            action=AuditAction.LOGIN,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
            ip_address=client_ip(request) if request else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )

    async def on_after_forgot_password(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        await record(
            self.user_db.session,  # type: ignore[attr-defined]
            action=AuditAction.PASSWORD_RESET_REQUEST,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
            ip_address=client_ip(request) if request else None,
        )
        await send_reset_password_email(user.email, token)

    async def on_after_reset_password(self, user: User, request: Request | None = None) -> None:
        # Stamping this invalidates every access token minted before now and
        # every refresh token, so a password reset really does end all sessions.
        from basivo_orch.auth.security.tokens import RevocationReason, revoke_all_for_user

        await self.user_db.update(user, {"password_changed_at": datetime.now(UTC)})
        session = self.user_db.session  # type: ignore[attr-defined]
        await revoke_all_for_user(session, user.id, reason=RevocationReason.PASSWORD_CHANGED)
        await record(
            session,
            action=AuditAction.PASSWORD_RESET_COMPLETE,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
            ip_address=client_ip(request) if request else None,
        )
        await send_password_changed_email(user.email)

    async def on_after_request_verify(
        self, user: User, token: str, request: Request | None = None
    ) -> None:
        await record(
            self.user_db.session,  # type: ignore[attr-defined]
            action=AuditAction.EMAIL_VERIFY_REQUEST,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
        )
        await send_verify_email(user.email, token)

    async def on_after_verify(self, user: User, request: Request | None = None) -> None:
        await record(
            self.user_db.session,  # type: ignore[attr-defined]
            action=AuditAction.EMAIL_VERIFY_COMPLETE,
            outcome=Outcome.SUCCESS,
            user_id=user.id,
        )


async def get_user_manager(
    user_db: UserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db, password_helper)
