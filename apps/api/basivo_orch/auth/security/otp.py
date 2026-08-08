"""One-time code generation, storage and verification.

Design constraints, and why each one matters:

* **Codes come from ``secrets``, never ``random``.** ``random`` is a Mersenne
  Twister: observing a few outputs lets an attacker reconstruct the state and
  predict every future code.
* **Only a hash is stored.** Redis is frequently less hardened than the primary
  database and is often shared; a dump must not yield live codes.
* **Single use, with an attempt budget.** A 6-digit code is one in a million,
  which is fine against 5 guesses and useless against unlimited ones. The
  budget is consumed atomically so parallel requests cannot overspend it.
* **Verification is constant-time** so response timing cannot leak a prefix.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum

import structlog
from redis.asyncio import Redis

from basivo_orch.auth.security.crypto import constant_time_equals, sha256_hex
from basivo_orch.auth.security.redis_client import namespaced
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)



class OTPPurpose(StrEnum):
    LOGIN = "login"
    VERIFY_EMAIL = "verify_email"
    STEP_UP = "step_up"


class OTPResult(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    """Attempt budget spent. The code is burned regardless of further guesses."""


@dataclass(frozen=True, slots=True)
class IssuedOTP:
    code: str
    ttl_seconds: int


def _key(purpose: OTPPurpose, identifier: str) -> str:
    digest = sha256_hex(identifier.strip().lower())[:32]
    return namespaced("otp", purpose.value, digest)


def generate_code(length: int) -> str:
    """A zero-padded decimal code from the OS CSPRNG.

    ``randbelow`` is uniform over the range; ``randint`` on a non-power-of-ten
    modulus would bias the low digits.
    """
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


async def issue(store_: Redis[str], *, identifier: str, purpose: OTPPurpose) -> IssuedOTP:
    """Generate and store a code, replacing any outstanding one.

    Replacing rather than accumulating is deliberate: several live codes for
    one address multiply an attacker's chances per guess.
    """
    settings = get_settings()
    code = generate_code(settings.otp_length)
    key = _key(purpose, identifier)
    pipeline = store_.pipeline()
    pipeline.delete(key)
    pipeline.hset(key, mapping={"hash": sha256_hex(code), "attempts": "0"})
    pipeline.expire(key, settings.otp_ttl_seconds)
    await pipeline.execute()

    return IssuedOTP(code=code, ttl_seconds=settings.otp_ttl_seconds)


async def verify(
    store_: Redis[str], *, identifier: str, purpose: OTPPurpose, code: str
) -> OTPResult:
    """Check a submitted code and consume it on success."""
    settings = get_settings()
    key = _key(purpose, identifier)
    stored = await store_.hgetall(key)
    if not stored:
        # Covers both "never issued" and "expired": the TTL removes the key,
        # and the two cases are indistinguishable to the caller by design.
        return OTPResult.EXPIRED

    # Increment first. Doing it after a failed comparison would let concurrent
    # requests each read the same count and collectively exceed the budget.
    attempts = await store_.hincrby(key, "attempts", 1)
    if attempts > settings.otp_max_attempts:
        await store_.delete(key)
        logger.warning("otp_attempts_exhausted", purpose=purpose.value)
        return OTPResult.EXHAUSTED

    if not constant_time_equals(stored.get("hash", ""), sha256_hex(code)):
        return OTPResult.INVALID

    # Correct: burn it. A code that survived its own successful use would be
    # replayable from an intercepted email.
    await store_.delete(key)
    return OTPResult.VALID


async def peek_ttl(store_: Redis[str], *, identifier: str, purpose: OTPPurpose) -> int:
    """Seconds until the outstanding code expires, or 0 when there is none.

    Lets the resend endpoint enforce a cooldown without revealing whether the
    address is registered.
    """
    remaining = await store_.ttl(_key(purpose, identifier))
    return max(0, remaining)
