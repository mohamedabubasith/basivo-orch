"""One-time code issuance and verification."""

from __future__ import annotations

import pytest

from basivo_orch.auth.security import otp
from basivo_orch.auth.settings import get_settings

pytestmark = pytest.mark.security

IDENTIFIER = "ada@example.com"


async def test_issued_code_verifies_once(state) -> None:
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)

    first = await otp.verify(
        state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=issued.code
    )
    assert first is otp.OTPResult.VALID

    # Burned on success. A code that survived its own use would be replayable
    # from an intercepted email.
    second = await otp.verify(
        state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=issued.code
    )
    assert second is otp.OTPResult.EXPIRED


async def test_code_has_configured_length_and_is_numeric(state) -> None:
    settings = get_settings()
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)
    assert len(issued.code) == settings.otp_length
    assert issued.code.isdigit()


async def test_plaintext_code_is_never_stored(state) -> None:
    """A dump of the store must not yield live codes."""
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)
    for key in await state.keys("*"):
        stored = await state.hgetall(key)
        assert issued.code not in stored.values()


async def test_attempts_are_exhausted_and_the_code_is_burned(state) -> None:
    """A 6-digit code is safe against 5 guesses and worthless against unlimited."""
    settings = get_settings()
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)

    wrong = "0" * settings.otp_length
    if wrong == issued.code:
        wrong = "1" * settings.otp_length

    for _ in range(settings.otp_max_attempts):
        result = await otp.verify(
            state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=wrong
        )
        assert result is otp.OTPResult.INVALID

    exhausted = await otp.verify(
        state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=wrong
    )
    assert exhausted is otp.OTPResult.EXHAUSTED

    # Even the correct code is dead once the budget is spent, so an attacker
    # cannot spend the budget and then get lucky.
    assert (
        await otp.verify(
            state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=issued.code
        )
        is otp.OTPResult.EXPIRED
    )


async def test_reissue_invalidates_the_previous_code(state) -> None:
    """Several live codes for one address multiply an attacker's odds per guess."""
    first = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)
    second = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)

    if first.code != second.code:
        assert (
            await otp.verify(
                state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=first.code
            )
            is otp.OTPResult.INVALID
        )

    assert (
        await otp.verify(
            state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=second.code
        )
        is otp.OTPResult.VALID
    )


async def test_codes_are_scoped_by_purpose(state) -> None:
    """A code emailed for verification must not authorise a login."""
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.VERIFY_EMAIL)

    assert (
        await otp.verify(
            state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN, code=issued.code
        )
        is otp.OTPResult.EXPIRED
    )


async def test_codes_are_scoped_by_identifier(state) -> None:
    issued = await otp.issue(state, identifier=IDENTIFIER, purpose=otp.OTPPurpose.LOGIN)

    assert (
        await otp.verify(
            state, identifier="mallory@example.com", purpose=otp.OTPPurpose.LOGIN, code=issued.code
        )
        is otp.OTPResult.EXPIRED
    )


async def test_verifying_without_an_issued_code_reports_expired(state) -> None:
    """"Never issued" and "expired" are indistinguishable by design: telling
    them apart reveals whether a code was ever sent to that address."""
    assert (
        await otp.verify(
            state, identifier="nobody@example.com", purpose=otp.OTPPurpose.LOGIN, code="000000"
        )
        is otp.OTPResult.EXPIRED
    )


def test_generated_codes_are_uniformly_distributed() -> None:
    """A smoke test that the CSPRNG path is not biased or constant.

    `random.randint` on a non-power-of-ten modulus biases low digits; a constant
    would be catastrophic. Neither survives 500 samples.
    """
    samples = [otp.generate_code(6) for _ in range(500)]
    assert all(len(code) == 6 and code.isdigit() for code in samples)
    assert len(set(samples)) > 400, "codes should not repeat at this rate"

    leading = {code[0] for code in samples}
    assert len(leading) >= 8, "leading digit should span most of 0-9"
