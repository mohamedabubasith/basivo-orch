"""OAuth2 / OpenID Connect client configuration.

Providers are only registered when their credentials are present, so an
unconfigured provider is absent from the API rather than present and broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import structlog

from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    name: str
    display_name: str
    client: Any
    is_verified_by_default: bool = False
    """Whether to trust the provider's assertion that the email is verified.

    Only ever true for providers that actually verify addresses and say so in
    the token. Getting this wrong is an account-takeover: an IdP that lets
    anyone claim ``victim@example.com`` would otherwise mint a verified account.
    """


@lru_cache(maxsize=1)
def oauth_clients() -> dict[str, OAuthProvider]:
    settings = get_settings()
    providers: dict[str, OAuthProvider] = {}

    if settings.google_client_id and settings.google_client_secret.get_secret_value():
        from httpx_oauth.clients.google import GoogleOAuth2

        providers["google"] = OAuthProvider(
            name="google",
            display_name="Google",
            client=GoogleOAuth2(
                settings.google_client_id,
                settings.google_client_secret.get_secret_value(),
            ),
            # Google returns `email_verified` and enforces it for its own domains.
            is_verified_by_default=True,
        )

    if settings.github_client_id and settings.github_client_secret.get_secret_value():
        from httpx_oauth.clients.github import GitHubOAuth2

        providers["github"] = OAuthProvider(
            name="github",
            display_name="GitHub",
            client=GitHubOAuth2(
                settings.github_client_id,
                settings.github_client_secret.get_secret_value(),
                scopes=["user:email"],
            ),
            # GitHub exposes unverified addresses on the emails endpoint, so the
            # address alone is not proof of control.
            is_verified_by_default=False,
        )

    if (
        settings.oidc_discovery_url
        and settings.oidc_client_id
        and settings.oidc_client_secret.get_secret_value()
    ):
        from httpx_oauth.clients.openid import OpenID

        providers["oidc"] = OAuthProvider(
            name="oidc",
            display_name="Single sign-on",
            client=OpenID(
                settings.oidc_client_id,
                settings.oidc_client_secret.get_secret_value(),
                settings.oidc_discovery_url,
                base_scopes=["openid", "email", "profile"],
            ),
            # Depends entirely on the tenant's IdP. Left false; flip it only
            # after confirming the IdP verifies addresses.
            is_verified_by_default=False,
        )

    if not providers:
        logger.info("no_oauth_providers_configured")

    return providers
