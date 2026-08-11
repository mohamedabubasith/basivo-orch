"""The email-confirmation gate.

An unconfirmed address means nobody has proved they can read the mailbox. That
matters here for a specific reason rather than a ceremonial one: a workspace is
addressed by email — invitations, ownership transfer and password recovery all
resolve to it. Letting an unproven address own a workspace means a typo'd or
squatted address can hold resources whose only recovery path points somewhere
its owner cannot read.

So this is a gate, not a notice. The previous shape — full access plus a banner
asking nicely — was the worst of both: it implied confirmation was optional,
which made the banner nagging, while leaving the actual risk in place.

It lives in one dependency because there is exactly one chokepoint worth
trusting. ``authz.require()`` is on every org-scoped route, so gating there
gates flows, runs, analytics and keys together, and a route added tomorrow is
covered by construction rather than by remembering.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from basivo_orch.auth.engine import current_active_user
from basivo_orch.auth.models import User
from basivo_orch.config import get_settings


async def current_app_user(user: User = Depends(current_active_user)) -> User:
    """Authenticated, active, and — unless configured otherwise — confirmed.

    The 403 carries a machine-readable header rather than only prose, so the
    browser can route to the confirmation screen instead of pattern-matching a
    message that translation would break.
    """
    if get_settings().REQUIRE_VERIFIED_EMAIL and not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Confirm your email address to use your workspace.",
            headers={"X-Email-Verification-Required": "true"},
        )
    return user
