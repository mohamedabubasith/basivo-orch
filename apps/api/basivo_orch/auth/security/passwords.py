"""Password hashing and policy.

Hashing is Argon2id via ``pwdlib``. Argon2id is memory-hard, which is what makes
GPU and ASIC cracking expensive; bcrypt is not. ``passlib`` — the historical
default — is unmaintained and does not import on Python 3.13+, which is why the
lint config bans it outright.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
import structlog
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 2.5

# OWASP Password Storage Cheat Sheet, Argon2id baseline: 19 MiB, t=2, p=1.
# Raised to 64 MiB here — the extra ~40ms per login is a good trade for
# roughly 3x the cracking cost.
_hasher = Argon2Hasher(
    memory_cost=65_536,  # KiB
    time_cost=3,
    parallelism=4,
)

# The list is ordered: the first hasher is used for new hashes, the rest are
# accepted for verification. That is what allows transparent upgrades from a
# legacy bcrypt corpus without forcing a password reset.
password_hash = PasswordHash((_hasher,))

COMMON_PASSWORDS = frozenset(
    {
        "password", "123456", "123456789", "qwerty", "abc123", "letmein",
        "welcome", "admin", "iloveyou", "monkey", "dragon", "sunshine",
        "princess", "football", "charlie", "passw0rd", "trustno1",
    }
)


@dataclass(slots=True)
class PasswordCheck:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _normalise(password: str) -> str:
    """NFKC-normalise so visually identical passwords hash identically.

    Without this, a password typed on an IME or with combining accents can fail
    to match the one that was registered from a different input method.
    """
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    return password_hash.hash(_normalise(password))


def verify_password(password: str, hashed: str) -> tuple[bool, str | None]:
    """Verify a password.

    Returns ``(is_valid, updated_hash)``. ``updated_hash`` is non-None when the
    stored hash used older parameters, so the caller can persist the stronger
    hash while it legitimately holds the plaintext.
    """
    return password_hash.verify_and_update(_normalise(password), hashed)


_DUMMY_PLAINTEXT = "basivo-auth-timing-equaliser-placeholder"


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Computed on first use, not at import.

    Argon2 at 64 MiB costs ~50 ms; doing that at import time would slow every
    process start, every test collection and every CLI invocation.
    """
    return hash_password(_DUMMY_PLAINTEXT)


def dummy_verify() -> None:
    """Burn one hash verification against a throwaway hash.

    Called on the "user does not exist" branch of login. Without it, a missing
    account returns in microseconds while a real one takes ~50 ms of Argon2 work,
    and that difference alone enumerates the whole user table.
    """
    password_hash.verify(_DUMMY_PLAINTEXT, _dummy_hash())


def check_policy(password: str, *, email: str | None = None) -> PasswordCheck:
    """Structural policy. Deliberately not a composition ruleset.

    Length and blocklists are what actually correlate with resistance to
    guessing; forced symbol/digit classes mostly produce `Password1!` and are
    no longer recommended by NIST SP 800-63B.
    """
    settings = get_settings()
    errors: list[str] = []
    candidate = _normalise(password)

    if len(candidate) < settings.password_min_length:
        errors.append(f"Must be at least {settings.password_min_length} characters.")
    if len(candidate) > settings.password_max_length:
        errors.append(f"Must be at most {settings.password_max_length} characters.")

    lowered = candidate.lower()
    if lowered in COMMON_PASSWORDS:
        errors.append("This password is among the most commonly used. Choose another.")

    if email:
        local_part = email.split("@")[0].lower()
        if local_part and local_part in lowered:
            errors.append("Must not contain your email address.")

    if candidate and len(set(candidate)) == 1:
        errors.append("Must not be a single repeated character.")

    return PasswordCheck(ok=not errors, errors=errors)


async def is_breached(password: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """Check Have I Been Pwned using k-anonymity.

    Only the first 5 hex characters of the SHA-1 hash are sent. The service
    returns every suffix sharing that prefix (~800 rows) and the comparison
    happens locally, so the password — and even its full hash — never leaves
    this process.
    """
    settings = get_settings()
    if not settings.password_check_breaches:
        return False

    digest = hashlib.sha1(_normalise(password).encode("utf-8")).hexdigest().upper()  # noqa: S324
    prefix, suffix = digest[:5], digest[5:]

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=HIBP_TIMEOUT_SECONDS)
    try:
        response = await client.get(
            HIBP_RANGE_URL.format(prefix=prefix),
            headers={"Add-Padding": "true", "User-Agent": settings.jwt_issuer},
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        # Availability of a third party must not gate our own registration flow
        # unless the operator has explicitly chosen to fail closed.
        logger.warning(
            "hibp_lookup_failed",
            error=str(exc),
            fail_open=settings.password_breach_fail_open,
        )
        return not settings.password_breach_fail_open
    finally:
        if owns_client:
            await client.aclose()

    for line in response.text.splitlines():
        candidate_suffix, _, count = line.partition(":")
        if candidate_suffix.strip() == suffix:
            # Padding rows are returned with a count of 0; they are decoys.
            return count.strip() != "0"
    return False


async def validate(password: str, *, email: str | None = None) -> PasswordCheck:
    """Full validation: structural policy, then the breach corpus."""
    result = check_policy(password, email=email)
    if not result.ok:
        return result

    if await is_breached(password):
        return PasswordCheck(
            ok=False,
            errors=[
                "This password has appeared in a known data breach. "
                "Choose one you have not used elsewhere."
            ],
        )
    return result
