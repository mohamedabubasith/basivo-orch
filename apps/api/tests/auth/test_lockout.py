"""Progressive account lockout."""

from __future__ import annotations

import pytest

from basivo_orch.auth.security import lockout
from basivo_orch.auth.settings import get_settings

pytestmark = pytest.mark.security


async def test_no_lockout_below_threshold(state) -> None:
    settings = get_settings()
    for _ in range(settings.lockout_threshold - 1):
        result = await lockout.record_failure(state, identifier="ada@example.com")
    assert result.locked is False


async def test_lockout_engages_at_threshold(state) -> None:
    settings = get_settings()
    for _ in range(settings.lockout_threshold):
        result = await lockout.record_failure(state, identifier="ada@example.com")
    assert result.locked is True
    assert result.retry_after_seconds > 0


async def test_backoff_is_exponential(state) -> None:
    """Each further failure roughly doubles the delay, so online guessing dies
    quickly while a user who mistyped twice notices nothing."""
    settings = get_settings()
    delays = []
    for _ in range(settings.lockout_threshold + 3):
        result = await lockout.record_failure(state, identifier="ada@example.com")
        if result.locked:
            delays.append(result.retry_after_seconds)

    assert len(delays) >= 3
    assert delays[1] > delays[0]
    assert delays[2] > delays[1]


async def test_backoff_is_capped(state) -> None:
    """Capped, never permanent.

    A permanent lock would turn this control into a denial-of-service weapon:
    anyone who knows a victim's address could lock them out forever.
    """
    settings = get_settings()
    result = None
    for _ in range(settings.lockout_threshold + 20):
        result = await lockout.record_failure(state, identifier="ada@example.com")
    assert result is not None
    assert result.retry_after_seconds <= settings.lockout_max_seconds


async def test_reset_clears_lockout(state) -> None:
    settings = get_settings()
    for _ in range(settings.lockout_threshold):
        await lockout.record_failure(state, identifier="ada@example.com")
    assert (await lockout.check(state, identifier="ada@example.com")).locked is True

    await lockout.reset(state, identifier="ada@example.com")
    assert (await lockout.check(state, identifier="ada@example.com")).locked is False


async def test_lockout_is_per_account(state) -> None:
    """Locking one account must not lock an unrelated one."""
    settings = get_settings()
    for _ in range(settings.lockout_threshold + 2):
        await lockout.record_failure(state, identifier="victim@example.com")

    assert (await lockout.check(state, identifier="victim@example.com")).locked is True
    assert (await lockout.check(state, identifier="bystander@example.com")).locked is False


async def test_lockout_identifier_is_case_insensitive(state) -> None:
    """Otherwise 'Ada@example.com' is a free extra attempt budget."""
    settings = get_settings()
    for _ in range(settings.lockout_threshold + 1):
        await lockout.record_failure(state, identifier="ada@example.com")

    assert (await lockout.check(state, identifier="ADA@EXAMPLE.COM")).locked is True
