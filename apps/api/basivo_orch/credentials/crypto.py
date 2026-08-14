"""Encryption at rest for provider credentials.

AES-256-GCM, keyed by an HKDF subkey of the app's own `SECRET_KEY` — the same
scheme already used for JWT and CSRF subkeys (see `Settings.subkey`), so this
adds no second secret to provision or rotate. GCM is authenticated: a row
tampered with in the database fails to decrypt rather than decrypting to
garbage that gets sent to a model provider as an API key.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from basivo_orch.auth.settings import get_settings

_NONCE_LEN = 12  # 96-bit, the size GCM is specified and optimised for.


class DecryptionError(Exception):
    """The ciphertext did not authenticate. Wrong key, or the row was tampered with."""


def _aead() -> AESGCM:
    return AESGCM(get_settings().subkey("credential-encryption", length=32))


def encrypt(plaintext: str) -> str:
    """Return `base64(nonce || ciphertext)` — one column, nothing else to store."""
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = _aead().encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt(token: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return _aead().decrypt(nonce, ciphertext, None).decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError("Could not decrypt this credential.") from exc
