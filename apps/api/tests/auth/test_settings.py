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
    "jwt_secret": "j" * 64,
    "refresh_token_secret": "r" * 64,
    "csrf_secret": "c" * 64,
}


def test_valid_configuration_constructs() -> None:
    assert Settings(**BASE) is not None


def test_placeholder_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(**{**BASE, "secret_key": "changeme"})


def test_short_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least"):
        Settings(**{**BASE, "jwt_secret": "too-short"})


def test_reused_secrets_are_rejected() -> None:
    """Distinct keys per purpose mean one leak does not compromise the others."""
    shared = "x" * 64
    with pytest.raises(ValidationError, match="must all differ"):
        Settings(**{**BASE, "secret_key": shared, "jwt_secret": shared})


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
