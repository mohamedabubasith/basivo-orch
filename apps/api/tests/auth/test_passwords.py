"""Password hashing and policy."""

from __future__ import annotations

import pytest

from basivo_orch.auth.security import passwords

pytestmark = pytest.mark.security


def test_hash_is_argon2id() -> None:
    """Argon2id specifically: memory-hard, so GPU cracking is expensive.

    bcrypt would pass a naive "is it hashed?" check while being orders of
    magnitude cheaper to attack at scale.
    """
    hashed = passwords.hash_password("correct-horse-battery-staple")
    assert hashed.startswith("$argon2id$")


def test_hashes_are_salted() -> None:
    """Identical passwords must not produce identical hashes.

    Without per-hash salt, one rainbow table breaks every user who chose the
    same password, and equal hashes reveal which accounts share one.
    """
    first = passwords.hash_password("same-password-for-both")
    second = passwords.hash_password("same-password-for-both")
    assert first != second


def test_verify_round_trip() -> None:
    hashed = passwords.hash_password("correct-horse-battery-staple")
    valid, _ = passwords.verify_password("correct-horse-battery-staple", hashed)
    assert valid is True

    invalid, _ = passwords.verify_password("wrong-password", hashed)
    assert invalid is False


def test_unicode_normalisation() -> None:
    """Visually identical passwords from different input methods must match.

    U+00E9 (precomposed) and 'e' + U+0301 (combining acute) render identically
    but are different byte sequences. Input methods on different platforms
    disagree about which they emit, so without NFKC a user can register on one
    machine and be unable to sign in from another.
    """
    composed = "caf\u00e9-password-1234"
    decomposed = "cafe\u0301-password-1234"
    assert composed != decomposed, "the literals must differ at the byte level"

    hashed = passwords.hash_password(composed)
    valid, _ = passwords.verify_password(decomposed, hashed)
    assert valid is True


def test_policy_rejects_short_passwords() -> None:
    result = passwords.check_policy("short")
    assert result.ok is False
    assert any("at least" in error for error in result.errors)


def test_policy_rejects_common_passwords() -> None:
    result = passwords.check_policy("password")
    assert result.ok is False


def test_policy_rejects_password_containing_email() -> None:
    result = passwords.check_policy("ada-lovelace-2026", email="ada@example.com")
    assert result.ok is False
    assert any("email" in error for error in result.errors)


def test_policy_rejects_single_repeated_character() -> None:
    result = passwords.check_policy("aaaaaaaaaaaaaaaa")
    assert result.ok is False


def test_policy_accepts_a_long_passphrase() -> None:
    """No composition rules. NIST SP 800-63B advises against forcing symbol
    and digit classes; length and a blocklist do the real work."""
    result = passwords.check_policy("correct horse battery staple")
    assert result.ok is True, result.errors


def test_policy_rejects_absurdly_long_input() -> None:
    """Unbounded input is a DoS: Argon2 cost scales with length."""
    result = passwords.check_policy("a-very-long-password" * 200)
    assert result.ok is False


async def test_breach_check_is_skipped_when_disabled(monkeypatch) -> None:
    from basivo_orch.auth.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PASSWORD_CHECK_BREACHES", "false")
    assert await passwords.is_breached("password") is False
    get_settings.cache_clear()


async def test_breach_check_fails_open_when_hibp_is_unreachable(monkeypatch) -> None:
    """A third party being down must not block registration by default."""
    import httpx

    from basivo_orch.auth.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("PASSWORD_CHECK_BREACHES", "true")
    monkeypatch.setenv("PASSWORD_BREACH_FAIL_OPEN", "true")

    class FailingClient:
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("network down")

        async def aclose(self) -> None:
            return None

    assert await passwords.is_breached("anything", client=FailingClient()) is False
    get_settings.cache_clear()


def test_dummy_verify_does_real_work() -> None:
    """The timing equaliser must actually hash, not short-circuit.

    If it were a no-op, the missing-account branch of login would return far
    faster than the wrong-password branch and enumerate every user.
    """
    import time

    start = time.perf_counter()
    passwords.dummy_verify()
    elapsed = time.perf_counter() - start
    assert elapsed > 0.005, "dummy verification should cost real Argon2 time"
