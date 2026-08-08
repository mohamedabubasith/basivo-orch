"""Request rate limiting.

Backed by Redis so limits are shared across every worker and pod. An in-process
limiter multiplies the real limit by the number of workers, which is the usual
reason "5/minute" quietly becomes "40/minute" in production.

Two keying strategies:

* by client IP — the general case
* by submitted identifier (email) — so one attacker rotating through a botnet
  still cannot spray a single account
"""

from __future__ import annotations

import ipaddress

import structlog
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from basivo_orch.auth.security.crypto import sign
from basivo_orch.auth.settings import Environment, get_settings

logger = structlog.get_logger(__name__)

TRUSTED_PROXY_HEADER = "x-forwarded-for"


def client_ip(request: Request) -> str:
    """Resolve the client address.

    ``X-Forwarded-For`` is attacker-controlled unless a proxy you trust rewrites
    it, so it is only consulted when ``trusted_proxy_count`` is configured. The
    Nth-from-the-right entry is the address your own edge saw; entries to the
    left of it were supplied by the client and must be ignored.
    """
    settings = get_settings()
    trusted = settings.trusted_proxy_count

    if trusted > 0:
        forwarded = request.headers.get(TRUSTED_PROXY_HEADER, "")
        parts = [item.strip() for item in forwarded.split(",") if item.strip()]
        if len(parts) >= trusted:
            candidate = parts[-trusted]
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                logger.warning("invalid_forwarded_ip", value=candidate)
            else:
                return candidate

    return get_remote_address(request) or "unknown"


def identifier_key(identifier: str) -> str:
    """Rate-limit bucket for an email address.

    HMAC'd rather than raw: these keys live in Redis, which is frequently less
    protected than the database and often shared, and a raw key set would be a
    user directory.
    """
    return sign(identifier.strip().lower(), purpose="ratelimit")[:32]


def _limiter_key(request: Request) -> str:
    return client_ip(request)


def _storage_uri() -> str:
    """Where rate-limit counters live.

    Counters must be shared across workers and pods — an in-process store
    silently multiplies every configured limit by the worker count. Under test
    there is one process, so in-memory keeps the suite container-free without
    weakening anything real.
    """
    settings = get_settings()
    if settings.environment is Environment.TEST:
        return "memory://"
    return settings.redis_url


limiter = Limiter(
    key_func=_limiter_key,
    storage_uri=_storage_uri(),
    strategy="fixed-window",
    # Emits X-RateLimit-* on responses. Note this makes `response: Response` a
    # required parameter on every handler carrying @limiter.limit — SlowAPI
    # raises without one. tests/test_ratelimit.py enforces that.
    headers_enabled=True,
    enabled=get_settings().rate_limit_enabled,
)


def email_scoped_key(request: Request) -> str:
    """Key on the submitted email when present, else the IP.

    Used by login and forgot-password so that distributed guessing against one
    account is limited even though each request comes from a different address.
    """
    email = getattr(request.state, "rate_limit_identifier", None)
    if email:
        return f"id:{identifier_key(str(email))}"
    return f"ip:{client_ip(request)}"
