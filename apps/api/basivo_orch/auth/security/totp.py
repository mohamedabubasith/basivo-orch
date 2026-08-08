"""TOTP second factor (RFC 6238) and recovery codes.

Three properties that separate a correct TOTP implementation from a broken one:

* **The seed is encrypted at rest.** It has to be recoverable to verify codes,
  so it cannot be hashed — but a database dump must not hand over a working
  second factor. Encrypted with a key derived from ``SECRET_KEY``.
* **Used time-steps are recorded.** A code stays valid for its whole 30-second
  window, so without a replay guard an attacker who observes one code (shoulder
  surfing, a phishing proxy) can reuse it. Tracking the highest accepted counter
  makes each code single-use.
* **Recovery codes are hashed and single-use.** They are password-equivalent:
  each one bypasses the second factor entirely.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import pyotp
import structlog

from basivo_orch.auth.security.crypto import decrypt, encrypt, sha256_hex
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)

TOTP_PURPOSE = "totp-seed"
TOTP_INTERVAL_SECONDS = 30

RECOVERY_CODE_GROUPS = 2
RECOVERY_CODE_GROUP_LENGTH = 5
"""Formatted as `xxxxx-xxxxx`. ~52 bits from the Crockford-ish alphabet below,
which is far beyond guessable while still being transcribable from paper."""

# No I, L, O, U, 0 or 1: they are the characters people mis-transcribe, and a
# recovery code is typically copied by hand from a printout under stress.
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


@dataclass(frozen=True, slots=True)
class Enrolment:
    secret: str
    encrypted_secret: str
    provisioning_uri: str


def generate_secret() -> str:
    """A fresh base32 seed (160 bits, the RFC 4226 recommendation)."""
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return encrypt(secret, purpose=TOTP_PURPOSE)


def decrypt_secret(encrypted: str) -> str | None:
    return decrypt(encrypted, purpose=TOTP_PURPOSE)


def start_enrolment(email: str) -> Enrolment:
    settings = get_settings()
    secret = generate_secret()
    uri = pyotp.TOTP(secret, interval=TOTP_INTERVAL_SECONDS).provisioning_uri(
        name=email,
        issuer_name=settings.totp_issuer,
    )
    return Enrolment(secret=secret, encrypted_secret=encrypt_secret(secret), provisioning_uri=uri)


def current_counter(timestamp: float | None = None) -> int:
    """The time-step index a code would be minted for right now."""
    import time

    now = timestamp if timestamp is not None else time.time()
    return int(now // TOTP_INTERVAL_SECONDS)


def verify_code(
    encrypted_secret: str,
    code: str,
    *,
    last_counter: int | None = None,
) -> tuple[bool, int | None]:
    """Verify a TOTP code.

    Returns ``(is_valid, counter)``. Persist ``counter`` as the user's
    ``totp_last_counter`` so the same code cannot be presented twice.
    """
    settings = get_settings()
    secret = decrypt_secret(encrypted_secret)
    if secret is None:
        # Ciphertext does not decrypt: either SECRET_KEY was rotated without
        # re-encrypting seeds, or the row was tampered with. Fail closed.
        logger.error("totp_secret_undecryptable")
        return False, None

    cleaned = code.strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        return False, None

    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL_SECONDS)
    window = settings.totp_window

    # pyotp.verify() with valid_window accepts a code but does not tell us which
    # step matched, and the step is exactly what the replay guard needs.
    now = current_counter()
    for offset in range(-window, window + 1):
        counter = now + offset
        candidate = totp.generate_otp(counter)
        if secrets.compare_digest(candidate, cleaned):
            if last_counter is not None and counter <= last_counter:
                # Already spent. This is the replay case.
                logger.warning("totp_replay_blocked", counter=counter, last=last_counter)
                return False, None
            return True, counter

    return False, None


def generate_recovery_codes(count: int | None = None) -> list[str]:
    settings = get_settings()
    total = count if count is not None else settings.totp_recovery_code_count
    codes = []
    for _ in range(total):
        groups = [
            "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_GROUP_LENGTH))
            for _ in range(RECOVERY_CODE_GROUPS)
        ]
        codes.append("-".join(groups))
    return codes


def normalise_recovery_code(code: str) -> str:
    """Canonicalise before hashing so formatting differences do not matter."""
    return code.strip().upper().replace(" ", "").replace("-", "")


def hash_recovery_code(code: str) -> str:
    return sha256_hex(normalise_recovery_code(code))


def render_qr_svg(provisioning_uri: str) -> str:
    """Inline SVG for the enrolment QR code.

    SVG rather than a PNG data URI so it stays crisp at any size, and generated
    server-side so the seed never has to be handed to a third-party QR service.
    """
    import io

    import qrcode
    import qrcode.image.svg

    image = qrcode.make(provisioning_uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")
