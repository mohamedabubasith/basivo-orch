"""Configuration guardrails.

A service that refuses to start is a far better outcome than one that starts
with a placeholder signing key. These tests assert the refusals actually fire.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from basivo_orch.auth.settings import Settings

pytestmark = pytest.mark.security

BASE = {
    "secret_key": "s" * 64,
}


def test_valid_configuration_constructs() -> None:
    assert Settings(**BASE) is not None


def test_placeholder_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(**{**BASE, "secret_key": "changeme"})


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least"):
        Settings(**{**BASE, "secret_key": "too-short"})


def test_subkeys_are_independent_per_purpose() -> None:
    """One configured secret, but no two purposes share a key.

    This is what replaces the old separate JWT_SECRET / CSRF_SECRET variables:
    the separation is still real, it is just derived rather than configured.
    """
    settings = Settings(**BASE)
    purposes = ["jwt", "csrf", "reset-password", "verify-email", "oauth-state"]
    keys = [settings.subkey(p) for p in purposes]

    assert len(set(keys)) == len(purposes)
    assert all(len(k) == 32 for k in keys)
    # and not simply the master secret handed out under different names
    assert settings.secret_key.get_secret_value().encode() not in keys


def test_subkeys_are_deterministic() -> None:
    """Two processes with the same SECRET_KEY must agree, or tokens minted by
    one instance would fail to verify on another."""
    assert Settings(**BASE).subkey("jwt") == Settings(**BASE).subkey("jwt")


def test_changing_the_master_secret_changes_every_subkey() -> None:
    a = Settings(**BASE)
    b = Settings(**{**BASE, "secret_key": "t" * 64})
    assert a.subkey("jwt") != b.subkey("jwt")
    assert a.subkey("csrf") != b.subkey("csrf")


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        Settings(
            **BASE,
            environment="production",
            debug=True,
            public_base_url="https://auth.example.com",
        )


def test_wildcard_cors_is_rejected_in_production() -> None:
    """Wildcard origin plus credentials is both browser-rejected and unsafe."""
    with pytest.raises(ValidationError, match="CORS"):
        Settings(
            **BASE,
            environment="production",
            cors_origins="*",
            public_base_url="https://auth.example.com",
        )


def test_plaintext_http_origin_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="HTTP"):
        Settings(
            **BASE,
            environment="production",
            cors_origins="http://app.example.com",
            public_base_url="https://auth.example.com",
        )


def test_plaintext_public_url_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            **BASE,
            environment="production",
            cors_origins="https://app.example.com",
            public_base_url="http://auth.example.com",
        )


def test_insecure_cookie_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(
            **BASE,
            environment="production",
            cookie_secure=False,
            cors_origins="https://app.example.com",
            public_base_url="https://auth.example.com",
        )


def test_samesite_none_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="SAMESITE"):
        Settings(
            **BASE,
            environment="production",
            cookie_samesite="none",
            cors_origins="https://app.example.com",
            public_base_url="https://auth.example.com",
        )


def test_development_allows_local_defaults() -> None:
    """The guardrails must not make local development impossible."""
    settings = Settings(
        **BASE,
        environment="development",
        debug=True,
        cors_origins="http://localhost:3000",
    )
    assert settings.debug is True


def test_access_token_ttl_is_bounded() -> None:
    """A long-lived access token cannot be revoked, so the ceiling is the control."""
    with pytest.raises(ValidationError):
        Settings(**BASE, access_token_ttl_seconds=86_400)


def test_comma_separated_origins_parse() -> None:
    settings = Settings(**BASE, cors_origins="http://a.test,http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]
