"""Authentication backends: how an access token is carried and validated.

A backend pairs a *transport* (where the token lives on the wire) with a
*strategy* (how it is minted and verified).
"""

from __future__ import annotations

import uuid

from fastapi_users.authentication import (
    AuthenticationBackend,
    CookieTransport,
    JWTStrategy,
)

from basivo_orch.auth.models import User
from basivo_orch.auth.settings import get_settings


def _cookie_transport() -> CookieTransport:
    settings = get_settings()
    return CookieTransport(
        cookie_name=settings.cookie_name,
        cookie_max_age=settings.access_token_ttl_seconds,
        cookie_domain=settings.cookie_domain,
        # HttpOnly is the entire point of the cookie transport: it removes
        # token theft via XSS, because script on the page cannot read it.
        cookie_httponly=True,
        cookie_secure=settings.cookie_secure,
        cookie_samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        cookie_path="/",
    )


def get_jwt_strategy() -> JWTStrategy[User, uuid.UUID]:
    """Access token strategy.

    The audience is this service's own ``jwt_audience``, deliberately *not* the
    library default of ``fastapi-users:auth``. Two reasons: a token minted by
    another fastapi-users service that happens to share a secret must not
    verify here, and it keeps the audience namespace consistent with the
    purpose-bound tokens in ``app.auth.security.tokens``.
    """
    settings = get_settings()
    return JWTStrategy(
        secret=settings.jwt_secret.get_secret_value(),
        lifetime_seconds=settings.access_token_ttl_seconds,
        token_audience=[settings.jwt_audience],
        algorithm=settings.jwt_algorithm,
    )


cookie_backend = AuthenticationBackend(
    name="cookie",
    transport=_cookie_transport(),
    get_strategy=get_jwt_strategy,
)


auth_backends = [
    cookie_backend,
]
