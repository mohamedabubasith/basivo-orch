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
        await _send_smtp(email)
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


async def _send_smtp(email: Email) -> None:
    from email.message import EmailMessage

    import aiosmtplib

    settings = get_settings()
    message = EmailMessage()
    message["From"] = f"{settings.email_from_name} <{settings.email_from}>"
    message["To"] = email.to
    message["Subject"] = email.subject
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user or None,
        password=settings.smtp_password.get_secret_value() or None,
        start_tls=settings.smtp_tls,
        timeout=10,
    )


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



