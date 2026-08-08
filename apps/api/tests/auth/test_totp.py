"""TOTP second factor."""

from __future__ import annotations

import time

import pyotp
import pytest

from basivo_orch.auth.security import totp
from basivo_orch.auth.settings import get_settings

pytestmark = pytest.mark.security


def test_enrolment_produces_a_usable_provisioning_uri() -> None:
    enrolment = totp.start_enrolment("ada@example.com")
    assert enrolment.provisioning_uri.startswith("otpauth://totp/")
    assert "secret=" in enrolment.provisioning_uri.lower()


def test_secret_is_encrypted_at_rest() -> None:
    """A database dump must not hand over a working second factor."""
    enrolment = totp.start_enrolment("ada@example.com")
    assert enrolment.secret not in enrolment.encrypted_secret
    assert totp.decrypt_secret(enrolment.encrypted_secret) == enrolment.secret


def test_valid_code_is_accepted() -> None:
    enrolment = totp.start_enrolment("ada@example.com")
    code = pyotp.TOTP(enrolment.secret, interval=totp.TOTP_INTERVAL_SECONDS).now()

    valid, counter = totp.verify_code(enrolment.encrypted_secret, code)
    assert valid is True
    assert counter == totp.current_counter()


def test_wrong_code_is_rejected() -> None:
    enrolment = totp.start_enrolment("ada@example.com")
    valid, _ = totp.verify_code(enrolment.encrypted_secret, "000000")
    assert valid is False


def test_replay_of_the_same_code_is_blocked() -> None:
    """A code stays valid for its whole 30-second window.

    Without the counter guard, anyone who observes one code — shoulder surfing,
    a phishing proxy, a screenshot — can reuse it within that window.
    """
    enrolment = totp.start_enrolment("ada@example.com")
    code = pyotp.TOTP(enrolment.secret, interval=totp.TOTP_INTERVAL_SECONDS).now()

    first, counter = totp.verify_code(enrolment.encrypted_secret, code)
    assert first is True

    replayed, _ = totp.verify_code(enrolment.encrypted_secret, code, last_counter=counter)
    assert replayed is False, "a spent time-step must not be accepted again"


def test_code_from_an_adjacent_window_is_accepted() -> None:
    """Tolerates clock skew, which is the single most common 2FA support ticket."""
    enrolment = totp.start_enrolment("ada@example.com")
    previous_counter = totp.current_counter() - 1
    code = pyotp.TOTP(enrolment.secret, interval=totp.TOTP_INTERVAL_SECONDS).generate_otp(
        previous_counter
    )

    valid, counter = totp.verify_code(enrolment.encrypted_secret, code)
    assert valid is True
    assert counter == previous_counter


def test_code_far_outside_the_window_is_rejected() -> None:
    enrolment = totp.start_enrolment("ada@example.com")
    stale = pyotp.TOTP(enrolment.secret, interval=totp.TOTP_INTERVAL_SECONDS).generate_otp(
        totp.current_counter() - 20
    )
    valid, _ = totp.verify_code(enrolment.encrypted_secret, stale)
    assert valid is False


def test_undecryptable_secret_fails_closed() -> None:
    """If SECRET_KEY was rotated without re-encrypting seeds, deny rather than
    silently allow."""
    valid, _ = totp.verify_code("not-a-valid-fernet-token", "123456")
    assert valid is False


def test_recovery_codes_are_distinct_and_well_formed() -> None:
    codes = totp.generate_recovery_codes(10)
    assert len(set(codes)) == 10
    for code in codes:
        left, _, right = code.partition("-")
        assert len(left) == totp.RECOVERY_CODE_GROUP_LENGTH
        assert len(right) == totp.RECOVERY_CODE_GROUP_LENGTH


def test_recovery_alphabet_excludes_confusable_characters() -> None:
    """Recovery codes get copied by hand from paper, usually under stress."""
    for character in "ILOU01":
        assert character not in totp.RECOVERY_ALPHABET


def test_recovery_code_hash_is_format_insensitive() -> None:
    """Users retype these with or without the dash, in either case."""
    code = "ABCDE-FGHJK"
    assert totp.hash_recovery_code(code) == totp.hash_recovery_code("abcdefghjk")
    assert totp.hash_recovery_code(code) == totp.hash_recovery_code(" ABCDE-FGHJK ")


def test_recovery_codes_are_stored_hashed_only() -> None:
    codes = totp.generate_recovery_codes(3)
    for code in codes:
        hashed = totp.hash_recovery_code(code)
        assert code not in hashed
        assert len(hashed) == 64  # SHA-256 hex


def test_qr_renders_svg() -> None:
    enrolment = totp.start_enrolment("ada@example.com")
    svg = totp.render_qr_svg(enrolment.provisioning_uri)
    assert "<svg" in svg


def test_counter_advances_with_time() -> None:
    now = time.time()
    assert totp.current_counter(now + 30) == totp.current_counter(now) + 1


# ---------------------------------------------------------------------------
# The step-up exchange, over HTTP
# ---------------------------------------------------------------------------


async def _sign_in(client, email: str, password: str) -> None:
    """Authenticate the test client.

    The session cookie is set explicitly rather than left to the jar. Cookies
    are issued with `Secure`, and the ASGI transport speaks plain `http://test`,
    so httpx stores them and then declines to send them — the request arrives
    unauthenticated and the test fails for a reason that has nothing to do with
    what it is checking. Setting it by hand is what a real browser over HTTPS
    would do anyway.
    """
    response = await client.post("/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    client.cookies.set(get_settings().cookie_name, response.json()["access_token"])


async def _enrol(client, session, user, password: str) -> str:
    """Sign in, turn on 2FA, and return the shared secret."""
    await _sign_in(client, user.email, password)
    started = await client.post("/auth/2fa/enrol")
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    confirmed = await client.post(
        "/auth/2fa/enrol/confirm", json={"code": pyotp.TOTP(secret).now()}
    )
    assert confirmed.status_code == 200, confirmed.text
    await session.refresh(user)

    # Confirming enrolment spends the current 30-second window, so the very
    # next code would be refused by the replay guard rather than by whatever
    # the caller is actually testing. Clearing the counter avoids a 30-second
    # sleep in the suite; the replay guard has its own tests above.
    user.totp_last_counter = None
    await session.commit()
    return secret


@pytest.mark.security
async def test_a_step_up_token_cannot_be_exchanged_twice(client, session, user, password) -> None:
    """One approval of the second factor buys exactly one session.

    The step-up token is a bearer credential with a five-minute life. If using
    it did not burn it, anyone who obtained a copy inside that window — from a
    proxy log, a crash report, a shared terminal — could mint further sessions
    for as long as they could also produce a code. Burning the jti on first use
    bounds the damage to the single exchange the user actually made.
    """
    secret = await _enrol(client, session, user, password)
    client.cookies.clear()

    response = await client.post(
        "/auth/login", data={"username": user.email, "password": password}
    )
    assert response.status_code == 401
    step_up = response.headers["X-Step-Up-Token"]

    totp_gen = pyotp.TOTP(secret)
    first = await client.post(
        "/auth/2fa/verify", json={"step_up_token": step_up, "code": totp_gen.now()}
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/auth/2fa/verify", json={"step_up_token": step_up, "code": totp_gen.now()}
    )

    # 401, specifically. The step-up token is claimed before the code is
    # checked, so a burned token fails here with 401. If the claim were ever
    # removed, this request would reach the code check instead and the TOTP
    # replay guard would answer 400 — which is why the exact status matters and
    # `>= 400` would not catch the regression.
    assert second.status_code == 401, second.text
