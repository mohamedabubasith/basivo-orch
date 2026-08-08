"""Shared Redis connection pool.

Redis holds the state that must be fast, shared across workers and naturally
expiring: OTP codes, rate-limit counters, lockout state and single-use nonces.
Putting these in Postgres would work but turns every login into extra write
load on the primary, and TTL-based expiry would need a sweeper job.
"""

from __future__ import annotations

from typing import cast

from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import Connection

from basivo_orch.auth.settings import get_settings

_pool: ConnectionPool[Connection] | None = None


def get_pool() -> ConnectionPool[Connection]:
    global _pool  # noqa: PLW0603
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=50,
            health_check_interval=30,
        )
    return _pool


def get_redis() -> Redis[str]:
    # The pool sets decode_responses=True, so every value comes back as str.
    # redis-py's overloads only infer that from a literal keyword on the
    # constructor itself, not from the pool, hence the cast.
    return cast("Redis[str]", Redis(connection_pool=get_pool()))


async def close_redis() -> None:
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


def namespaced(*parts: str) -> str:
    """Build a key namespaced by issuer.

    Sharing a Redis instance between environments is common and usually
    accidental; the prefix keeps staging from expiring production's lockouts.
    """
    return ":".join((get_settings().jwt_issuer, *parts))
