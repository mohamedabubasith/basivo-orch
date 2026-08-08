"""Security response headers and CSRF protection."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from basivo_orch.auth.security.crypto import constant_time_equals, sign, verify_signature
from basivo_orch.auth.settings import Environment, get_settings

# This API returns JSON, never HTML, so the CSP can be maximally restrictive:
# nothing is allowed to load or execute at all. It still matters, because it
# neutralises reflected content should a response ever be rendered as a
# document (for example when a browser sniffs an error page).
API_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

# Interactive docs need their own bundle, so they get a narrower exception.
DOCS_CSP = (
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' "
    "https://cdn.jsdelivr.net; frame-ancestors 'none'; base-uri 'none'"
)

DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers to every response."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._settings = get_settings()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)

        is_docs = request.url.path.startswith(DOCS_PATHS)
        response.headers["Content-Security-Policy"] = DOCS_CSP if is_docs else API_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        )
        # Auth responses carry tokens and account state; caching any of it —
        # in the browser or an intermediary — risks serving one user's data to
        # the next.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"

        if self._settings.environment.is_production_like:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )

        # Server fingerprinting aids targeted exploitation. This removes
        # anything the application set; note that uvicorn appends its own
        # `Server` header *after* the ASGI app returns, so that one can only be
        # suppressed with `--no-server-header` on the server itself (the
        # generated Dockerfile does this).
        for header in ("server", "x-powered-by"):
            if header in response.headers:
                del response.headers[header]
        return response


# ---------------------------------------------------------------------------
# CSRF (double-submit cookie)
#
# Cookies are attached by the browser automatically, which is what makes a
# cookie session vulnerable to cross-site form posts. The defence: also require
# the value in a header. A cross-site attacker can cause the cookie to be sent
# but cannot read it to set the header, because the same-origin policy stops
# them reading our response.
#
# The cookie value is HMAC-signed so a forged pair fails even if an attacker
# can set cookies on a sibling subdomain.
# ---------------------------------------------------------------------------

CSRF_COOKIE_NAME = "basivo_orch_api_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"  # a header name, not a secret
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_PURPOSE = "csrf"


def issue_csrf_token() -> str:
    """Mint a signed CSRF token: ``<nonce>.<signature>``.

    The client copies this value verbatim from the cookie into the
    ``X-CSRF-Token`` header — cookie and header are the same string, which is
    what makes the double-submit check trivial to implement on the frontend.
    """
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{sign(nonce, purpose=CSRF_PURPOSE)}"


def set_csrf_cookie(response: Response) -> str:
    """Set the CSRF cookie and return its value for the client to echo back."""
    settings = get_settings()
    cookie_value = issue_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=cookie_value,
        max_age=settings.refresh_token_ttl_seconds,
        # Intentionally readable by JavaScript: the SPA copies it into the
        # request header. Its secrecy is not what provides the protection —
        # the same-origin policy is.
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        domain=settings.cookie_domain,
        path="/",
    )
    return cookie_value


def validate_csrf(request: Request) -> None:
    """Raise 403 unless the request carries a matching, signed CSRF pair."""
    settings = get_settings()
    if request.method in CSRF_SAFE_METHODS:
        return
    if settings.environment is Environment.TEST:
        return

    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    header_value = request.headers.get(CSRF_HEADER_NAME)

    if not cookie_value or not header_value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing.",
        )

    nonce, _, signature = cookie_value.partition(".")
    if not signature or not verify_signature(nonce, signature, purpose=CSRF_PURPOSE):
        # The cookie was not minted by us. Catches an attacker who can set
        # cookies on a sibling subdomain but cannot forge our HMAC.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token is invalid.",
        )

    if not constant_time_equals(cookie_value, header_value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch.",
        )


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce CSRF on cookie-authenticated mutating requests.

    Requests carrying an ``Authorization`` header are exempt: a bearer token is
    not attached automatically by the browser, so there is nothing to forge.
    """

    def __init__(self, app: ASGIApp, *, exempt_paths: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        self._exempt = exempt_paths

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.method not in CSRF_SAFE_METHODS
            and request.url.path not in self._exempt
            and "authorization" not in request.headers
            and get_settings().environment is not Environment.TEST
        ):
            try:
                validate_csrf(request)
            except HTTPException as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        return await call_next(request)
