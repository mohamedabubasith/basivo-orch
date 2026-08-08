"""Shared cryptographic primitives.

Rules this module exists to enforce:

* Anything compared against user input uses a constant-time comparison, so an
  attacker cannot recover a secret one byte at a time from response timing.
* Every purpose gets its own derived key via HKDF. Reusing one key across
  token signing, CSRF and at-rest encryption means one leak compromises all of
  them; derivation makes them independent at no operational cost.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from basivo_orch.auth.settings import get_settings

TOKEN_BYTES: Final = 32
"""256 bits. Opaque tokens must be infeasible to guess, not merely unique."""


def random_token() -> str:
    """A URL-safe opaque token from the OS CSPRNG."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def sha256_hex(value: str) -> str:
    """Digest used for at-rest storage of opaque tokens.

    A plain (unsalted, un-stretched) SHA-256 is correct here and would be wrong
    for passwords. These inputs are 256-bit random values, so there is no
    dictionary to attack and nothing for a work factor to buy.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def derive_key(purpose: str, *, length: int = 32) -> bytes:
    """Derive a purpose-bound key from the master secret.

    Thin wrapper over `Settings.subkey`, kept so callers in this package do not
    each have to reach for settings. One derivation implementation, so a change
    to the labelling scheme cannot apply to some keys and not others.
    """
    return get_settings().subkey(purpose, length=length)


@lru_cache(maxsize=8)
def _fernet(purpose: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key(purpose)))


def encrypt(plaintext: str, *, purpose: str) -> str:
    """Authenticated symmetric encryption for secrets that must be recoverable.

    Used for TOTP seeds: the server has to be able to read them back to verify
    codes, so they cannot be hashed, but a database dump alone must not yield a
    working second factor.
    """
    return _fernet(purpose).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, *, purpose: str) -> str | None:
    """Return the plaintext, or None if the ciphertext is invalid or foreign."""
    try:
        return _fernet(purpose).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def sign(value: str, *, purpose: str) -> str:
    """Detached HMAC tag, hex encoded."""
    return hmac.new(derive_key(purpose), value.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(value: str, tag: str, *, purpose: str) -> bool:
    return hmac.compare_digest(sign(value, purpose=purpose), tag)
