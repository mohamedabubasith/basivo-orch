"""Transactional email delivery.

The provider is chosen at generation time, but every send goes through
:func:`send` so retry, logging and the "never raise into the request path"
guarantee are implemented once.

Delivery failures are logged, not raised. A user who registers successfully but
whose welcome email bounces should still have an account; surfacing the SMTP
error would also leak infrastructure detail to an unauthenticated caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    # Autoescaping is mandatory: these templates interpolate user-controlled
    # values (display names, addresses) into HTML that lands in an inbox.
    autoescape=select_autoescape(["html", "xml"]),
    enable_async=True,
)


@dataclass(frozen=True, slots=True)
class Email:
    to: str
    subject: str
    html: str
    text: str


async def render(template: str, subject: str, to: str, **context: object) -> Email:
    settings = get_settings()
    base = {
        "project_name": settings.project_name,
        "frontend_base_url": str(settings.frontend_base_url).rstrip("/"),
        "support_email": settings.email_from,
    }
    html = await _env.get_template(f"{template}.html.j2").render_async(**base, **context)
    text = await _env.get_template(f"{template}.txt.j2").render_async(**base, **context)
    return Email(to=to, subject=subject, html=html, text=text)


async def send(email: Email) -> bool:
    """Dispatch one email. Returns success; never raises."""
    settings = get_settings()
    try:
        await _send_webhook(email)
    except Exception as exc:  # noqa: BLE001 - delivery must not break the flow
        logger.error(
            "email_send_failed",
            provider=settings.email_provider,
            subject=email.subject,
            error=str(exc),
        )
        return False

    logger.info("email_sent", provider=settings.email_provider, subject=email.subject)
    return True


async def _send_webhook(email: Email) -> None:
    """POST the rendered email to a URL you control, which does the sending.

    Built for an automation platform — n8n, Make, Zapier — so the mailbox can
    be one this service holds no credentials for: connect Gmail to n8n over
    OAuth, and all this service ever knows is a URL.

    The request is signed. An automation webhook is a URL and URLs leak — into
    browser history, a screenshot, an exported workflow — and an unauthenticated
    one lets whoever finds it send mail from your domain with content of their
    choosing. That is a phishing kit, addressed from you. Verifying the
    signature on the other end is what makes the endpoint safe to expose.
    """
    import hashlib
    import hmac
    import json
    import time

    import httpx

    settings = get_settings()

    payload = {
        "to": email.to,
        "subject": email.subject,
        "html": email.html,
        "text": email.text,
        "from": {"name": settings.email_from_name, "email": settings.email_from},
        "project": settings.project_name,
    }
    # Serialised once, and the signature covers these exact bytes. Re-encoding
    # before sending would let key order or spacing differ from what was
    # signed, and the receiver's check would fail intermittently.
    #
    # The form is deliberately canonical — sorted keys, no spaces, UTF-8 rather
    # than \uXXXX escapes. Verifying against the raw body is still the correct
    # approach, but not every receiver can reach it: n8n only exposes the raw
    # body when a specific option is set. Canonical output means such a
    # receiver can rebuild these exact bytes from the parsed JSON instead.
    # `ensure_ascii=False` is the load-bearing part: with the default, an
    # em-dash in an email template becomes \u2014 here and stays literal in
    # JavaScript, and every signature check fails on exactly the messages that
    # contain one.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )

    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "basivo-orch-api/auth",
        # Lets the receiver reject an old capture even if it never sees the
        # same request twice.
        "X-Basivo-Timestamp": timestamp,
    }

    secret = settings.email_webhook_secret.get_secret_value()
    if secret:
        signature = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Basivo-Signature"] = f"sha256={signature}"

    auth = settings.email_webhook_auth_header.get_secret_value()
    if auth:
        headers["Authorization"] = auth

    async with httpx.AsyncClient(timeout=settings.email_webhook_timeout_seconds) as client:
        response = await client.post(
            settings.email_webhook_url,
            content=body,
            headers=headers,
            # Not followed. A redirect would forward the body — which contains a
            # password-reset link — to a host that was never configured here.
            follow_redirects=False,
        )
        response.raise_for_status()


# ---------------------------------------------------------------------------
# Flow-specific helpers
#
# Links point at the frontend, not this API. The frontend collects the new
# password and calls back — so the token never lands in a page this service
# renders, and never appears in this service's access logs as a query string.
# ---------------------------------------------------------------------------


def _frontend_link(path: str, token: str) -> str:
    base = str(get_settings().frontend_base_url).rstrip("/")
    return f"{base}/{path.lstrip('/')}?token={quote(token, safe='')}"


async def send_verify_email(to: str, token: str) -> bool:
    settings = get_settings()
    email = await render(
        "verify_email",
        subject=f"Confirm your {settings.project_name} email address",
        to=to,
        action_url=_frontend_link("auth/verify", token),
        expires_hours=max(1, settings.verify_email_token_ttl_seconds // 3600),
    )
    return await send(email)


async def send_reset_password_email(to: str, token: str) -> bool:
    settings = get_settings()
    email = await render(
        "reset_password",
        subject=f"Reset your {settings.project_name} password",
        to=to,
        action_url=_frontend_link("auth/reset-password", token),
        expires_minutes=max(1, settings.reset_password_token_ttl_seconds // 60),
    )
    return await send(email)


async def send_password_changed_email(to: str) -> bool:
    """Notify after a successful change.

    This is a security control, not a courtesy: it is often the only way a user
    learns their account was taken over, while there is still time to act.
    """
    settings = get_settings()
    email = await render(
        "password_changed",
        subject=f"Your {settings.project_name} password was changed",
        to=to,
    )
    return await send(email)


async def send_otp_email(to: str, code: str, *, purpose: str = "sign in") -> bool:
    settings = get_settings()
    email = await render(
        "otp_code",
        subject=f"{code} is your {settings.project_name} code",
        to=to,
        code=code,
        purpose=purpose,
        expires_minutes=max(1, settings.otp_ttl_seconds // 60),
    )
    return await send(email)
