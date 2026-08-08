"""Progressive account lockout.

Lockout is deliberately *temporary and exponential* rather than permanent.
A permanent lock turns a rate-limit control into a denial-of-service weapon:
anyone who knows a victim's email can lock them out indefinitely. Exponential
backoff makes online guessing hopeless while self-healing for the real user.

Counters are keyed by both account and client IP. The account key stops
credential stuffing against one user; the IP key stops one host spraying many
accounts.

"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from redis.asyncio import Redis

from basivo_orch.auth.security.redis_client import namespaced
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)



@dataclass(frozen=True, slots=True)
class LockoutState:
    locked: bool
    retry_after_seconds: int = 0
    failure_count: int = 0


def _account_key(identifier: str) -> str:
    return namespaced("lockout", "account", identifier.lower())


def _ip_key(ip_address: str) -> str:
    return namespaced("lockout", "ip", ip_address)


def _backoff_seconds(failures: int) -> int:
    """Exponential backoff, capped.

    With the defaults (threshold 5, base 60s, cap 3600s) the delays run
    60s, 120s, 240s … so ten failures already cost over an hour, while a user
    who mistypes twice notices nothing.
    """
    settings = get_settings()
    if failures < settings.lockout_threshold:
        return 0
    excess = failures - settings.lockout_threshold
    delay = settings.lockout_base_seconds * (2**excess)
    return int(min(delay, settings.lockout_max_seconds))


async def check(
    store_: Redis[str],
    *,
    identifier: str,
    ip_address: str | None = None,
) -> LockoutState:
    """Report whether this identifier or IP is currently locked out."""
    keys = [_account_key(identifier)]
    if ip_address:
        keys.append(_ip_key(ip_address))

    worst = LockoutState(locked=False)
    for key in keys:
        remaining = await store_.ttl(f"{key}:until")
        if remaining and remaining > 0:
            failures = int(await store_.get(key) or 0)
            if remaining > worst.retry_after_seconds:
                worst = LockoutState(
                    locked=True, retry_after_seconds=remaining, failure_count=failures
                )
    return worst


async def record_failure(
    store_: Redis[str],
    *,
    identifier: str,
    ip_address: str | None = None,
) -> LockoutState:
    """Count a failed attempt and apply backoff once the threshold is crossed."""
    settings = get_settings()
    account_key = _account_key(identifier)
    # The counter itself expires, so a user who fails twice today and once next
    # week is never treated as a three-failure attacker.
    window = settings.lockout_max_seconds * 2

    pipeline = store_.pipeline()
    pipeline.incr(account_key)
    pipeline.expire(account_key, window)
    if ip_address:
        pipeline.incr(_ip_key(ip_address))
        pipeline.expire(_ip_key(ip_address), window)
    results = await pipeline.execute()
    failures = int(results[0])

    delay = _backoff_seconds(failures)
    if delay:
        await store_.set(f"{account_key}:until", "1", ex=delay)
        logger.warning(
            "account_locked",
            failure_count=failures,
            retry_after_seconds=delay,
            ip_address=ip_address,
        )
        return LockoutState(locked=True, retry_after_seconds=delay, failure_count=failures)
    return LockoutState(locked=False, failure_count=failures)


async def reset(
    store_: Redis[str],
    *,
    identifier: str,
    ip_address: str | None = None,
) -> None:
    """Clear counters after a successful authentication."""
    account_key = _account_key(identifier)
    keys = [account_key, f"{account_key}:until"]
    if ip_address:
        keys.append(_ip_key(ip_address))
    await store_.delete(*keys)
