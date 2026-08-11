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

**A gate nobody can pass is a lockout, not a gate.** The first version enforced
unconditionally and locked a real account out of a deployment whose mail was not
configured: sign-in worked, the wall appeared, "resend" reported success because
the API answers 202 either way, and nothing ever arrived. Requesting proof the
user has no way to produce is not a security control — it is an outage that
looks like one. So enforcement is now conditional on delivery being possible,
and the condition is checked rather than assumed.

It lives in one dependency because there is exactly one chokepoint worth
trusting. ``authz.require()`` is on every org-scoped route, so gating there
gates flows, runs, analytics and keys together, and a route added tomorrow is
covered by construction rather than by remembering.
"""

from __future__ import annotations

import structlog
from fastapi import Depends, HTTPException, status

from basivo_orch.auth.engine import current_active_user
from basivo_orch.auth.models import User
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.config import get_settings

logger = structlog.get_logger(__name__)


def email_is_deliverable() -> bool:
    """Whether this deployment can actually send a confirmation link.

    The webhook URL is the whole delivery path — with it unset, every
    ``send()`` logs ``email_send_failed`` and returns False, and no amount of
    pressing "resend" will ever produce a link.
    """
    return bool(get_auth_settings().email_webhook_url)


def gate_is_active() -> bool:
    """Enforce only when the gate is both wanted and satisfiable."""
    return get_settings().REQUIRE_VERIFIED_EMAIL and email_is_deliverable()


async def current_app_user(user: User = Depends(current_active_user)) -> User:
    """Authenticated, active, and — when the gate is active — confirmed.

    The 403 carries a machine-readable header rather than only prose, so the
    browser can route to the confirmation screen instead of pattern-matching a
    message that translation would break.
    """
    if not user.is_verified and gate_is_active():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Confirm your email address to use your workspace.",
            headers={"X-Email-Verification-Required": "true"},
        )
    return user


def warn_if_gate_is_inert() -> None:
    """Say so at startup, loudly, rather than letting it be discovered later.

    Silently not enforcing a control that is switched on is how a deployment
    ends up believing it has a property it does not have.
    """
    if get_settings().REQUIRE_VERIFIED_EMAIL and not email_is_deliverable():
        logger.warning(
            "email_gate_inert",
            reason="REQUIRE_VERIFIED_EMAIL is on but EMAIL_WEBHOOK_URL is unset",
            effect=(
                "Unconfirmed accounts are being admitted, because they have no "
                "way to confirm. Set EMAIL_WEBHOOK_URL to enforce the gate, or "
                "REQUIRE_VERIFIED_EMAIL=false to make the intent explicit."
            ),
        )
