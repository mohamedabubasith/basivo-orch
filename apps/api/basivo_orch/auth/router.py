"""Mount point for the auth package.

Everything this package exposes reaches your application through here. Wire it
into your existing FastAPI app with three lines::

    from fastapi import FastAPI
    from basivo_orch.auth.router import auth_router, install_auth

    app = FastAPI()
    install_auth(app)              # middleware: security headers, CSRF, rate limits
    app.include_router(auth_router)

``install_auth`` is separate from ``include_router`` on purpose: middleware
order is a security property, not a detail. See the note in that function.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from basivo_orch.auth.engine import auth_backends, fastapi_users, oauth_clients
from basivo_orch.auth.routers import orgs as orgs_router
from basivo_orch.auth.routers import otp as otp_router
from basivo_orch.auth.routers import password as password_router
from basivo_orch.auth.routers import session as session_router
from basivo_orch.auth.routers import sso as sso_router
from basivo_orch.auth.routers import totp as totp_router
from basivo_orch.auth.schemas import UserCreate, UserRead, UserUpdate
from basivo_orch.auth.security.headers import CSRFMiddleware, SecurityHeadersMiddleware
from basivo_orch.auth.security.ratelimit import limiter
from basivo_orch.auth.settings import get_settings

__all__ = ["auth_router", "install_auth"]


def _build_router() -> APIRouter:
    settings = get_settings()
    router = APIRouter()

    # Note: the engine's own auth router is deliberately not mounted. Its login
    # route cannot issue a refresh token, cannot be rate limited and does not
    # know about the second factor — see basivo_orch.auth/routers/session.py.
    router.include_router(session_router.router)
    router.include_router(password_router.router)

    router.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/auth",
        tags=["auth"],
    )
    router.include_router(
        fastapi_users.get_verify_router(UserRead),
        prefix="/auth",
        tags=["auth"],
    )
    router.include_router(
        fastapi_users.get_users_router(UserRead, UserUpdate),
        prefix="/users",
        tags=["users"],
    )
    router.include_router(otp_router.router)
    router.include_router(totp_router.router)
    router.include_router(orgs_router.router)
    router.include_router(sso_router.router)

    for provider in oauth_clients().values():
        router.include_router(
            fastapi_users.get_oauth_router(
                provider.client,
                auth_backends[0],
                settings.subkey_str("oauth-state"),
                redirect_url=None,
                # Never link an OAuth identity to an existing local account
                # purely because the addresses match: with an IdP that does not
                # verify email, that is a one-request account takeover.
                associate_by_email=settings.sso_auto_link_verified_emails
                and provider.is_verified_by_default,
                is_verified_by_default=provider.is_verified_by_default,
            ),
            prefix=f"/auth/{provider.name}",
            tags=["sso"],
        )
        router.include_router(
            fastapi_users.get_oauth_associate_router(
                provider.client, UserRead, settings.subkey_str("oauth-associate")
            ),
            prefix=f"/auth/associate/{provider.name}",
            tags=["sso"],
        )

    return router


#: Every auth route. Mount with ``app.include_router(auth_router)``.
auth_router: APIRouter = _build_router()


def install_auth(app: FastAPI, *, csrf_exempt_prefixes: tuple[str, ...] = ()) -> None:
    """Attach the middleware auth depends on.

    Call this **before** adding your own middleware. Starlette runs middleware
    in the order added for requests and in reverse for responses, so adding the
    security-header middleware first is what guarantees its headers reach every
    response — including ones short-circuited by CSRF or the rate limiter.

    Deliberately does not touch CORS: your application already owns that, and
    silently replacing your policy would be worse than making you extend it.
    Auth needs these on your existing CORSMiddleware::

        allow_credentials=True,
        allow_headers=[..., "X-CSRF-Token"],
        expose_headers=[..., "X-CSRF-Token", "X-Step-Up-Token", "Retry-After"],
    """
    settings = get_settings()

    app.state.limiter = limiter
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CSRFMiddleware,
        # Path prefixes your own API owns that are authenticated by something
        # the browser does not attach automatically — an API key, an HMAC
        # signature. CSRF does not apply there. Pass nothing if every route in
        # your app can be authenticated by a session cookie.
        exempt_prefixes=csrf_exempt_prefixes,
        # These bootstrap a session, so there is no CSRF cookie to present yet.
        # Safe to exempt: none performs a state change an attacker gains from
        # forging, and each is independently rate limited.
        exempt_paths=frozenset(
            {
                "/auth/login",
                "/auth/refresh",
                "/auth/register",
                "/auth/forgot-password",
                "/auth/reset-password",
            }
        ),
    )

    if settings.rate_limit_enabled:
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        app.add_middleware(SlowAPIMiddleware)

        if RateLimitExceeded not in app.exception_handlers:
            from fastapi import Request, status
            from fastapi.responses import JSONResponse

            async def _rate_limited(request: Request, exc: Exception) -> JSONResponse:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "Too many requests. Please slow down."},
                    headers={"Retry-After": "60"},
                )

            app.add_exception_handler(RateLimitExceeded, _rate_limited)
