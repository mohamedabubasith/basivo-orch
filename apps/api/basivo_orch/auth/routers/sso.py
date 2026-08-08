"""SSO discovery and redirect-target validation.

The OAuth authorize/callback routes themselves come from the engine and are
mounted in ``app.main``. This module adds what the engine does not provide:
a provider listing for the frontend, and a strict allowlist for post-login
redirects.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, status

from basivo_orch.auth.engine import oauth_clients
from basivo_orch.auth.schemas import SSOProvider
from basivo_orch.auth.settings import get_settings

router = APIRouter(prefix="/auth/sso", tags=["sso"])


@router.get("/providers", response_model=list[SSOProvider])
async def list_providers() -> list[SSOProvider]:
    """Providers that are actually configured.

    Lets the frontend render only the buttons that will work, instead of
    hard-coding a list that drifts from the deployment's environment.
    """
    return [
        SSOProvider(
            name=provider.name,
            display_name=provider.display_name,
            authorize_path=f"/auth/{provider.name}/authorize",
        )
        for provider in oauth_clients().values()
    ]


def validate_redirect_url(candidate: str) -> str:
    """Reject any redirect target not on the configured allowlist.

    An open redirect on a login callback is a full account takeover: the
    attacker sends the victim through a legitimate login and has the resulting
    code or token delivered to a host they control.

    Matching is exact against ``SSO_ALLOWED_REDIRECT_URLS``. Prefix or
    suffix matching is not safe here — ``https://good.com.evil.net`` passes a
    naive prefix check, and ``https://evil.com/?x=https://good.com`` passes a
    naive "contains" check.
    """
    settings = get_settings()
    allowed = set(settings.sso_allowed_redirect_urls)

    if candidate in allowed:
        return candidate

    # Also permit an exact origin+path match ignoring query and fragment, so a
    # frontend can round-trip its own state without every variant being listed.
    parsed = urlparse(candidate)
    normalised = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if normalised in allowed:
        return candidate

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Redirect target is not allowed.",
    )
