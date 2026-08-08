"""Append-only audit logging.

Every authentication-relevant action lands here with actor, source and outcome.
Two rules make this safe to keep long-term:

* Payloads are redacted before write. Audit logs are read by more people, and
  retained for longer, than any other table — a password or token that lands in
  one is a durable leak.
* Email addresses are stored as a keyed HMAC, not plaintext, on the failure
  paths where the account may not even exist. That still lets you group
  attempts by target without turning the audit table into a user directory.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import AuditEvent
from basivo_orch.auth.security.crypto import sign
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)

REDACTED = "[redacted]"

# Substring match, case-insensitive: catches `password`, `new_password`,
# `hashed_password`, `totp_secret`, `refresh_token` and friends in one rule.
SENSITIVE_KEY_FRAGMENTS = (
    "password", "secret", "token", "authorization", "cookie",
    "otp", "code", "credential", "key", "session",
)

MAX_DETAIL_BYTES = 4096


class AuditAction(StrEnum):
    REGISTER = "register"
    LOGIN = "login"
    LOGOUT = "logout"
    TOKEN_REFRESH = "token_refresh"
    TOKEN_REUSE_DETECTED = "token_reuse_detected"
    PASSWORD_CHANGE = "password_change"
    PASSWORD_RESET_REQUEST = "password_reset_request"
    PASSWORD_RESET_COMPLETE = "password_reset_complete"
    EMAIL_VERIFY_REQUEST = "email_verify_request"
    EMAIL_VERIFY_COMPLETE = "email_verify_complete"
    ACCOUNT_LOCKED = "account_locked"
    OTP_SENT = "otp_sent"
    OTP_VERIFIED = "otp_verified"
    TOTP_ENROLLED = "totp_enrolled"
    TOTP_VERIFIED = "totp_verified"
    TOTP_DISABLED = "totp_disabled"
    RECOVERY_CODE_USED = "recovery_code_used"
    SSO_LOGIN = "sso_login"
    SSO_ACCOUNT_LINKED = "sso_account_linked"
    SSO_LINK_REJECTED = "sso_link_rejected"
    AUTHZ_ESCALATION_BLOCKED = "authz_escalation_blocked"
    """An escalation guard fired: someone with role-management authority tried to
    grant or act on a role above their own. Alert on this one."""

    AUTHZ_DENIED = "authz_denied"
    """A permission check failed. Clusters of these on one account are the
    signature of an attacker probing for over-granted authority."""

    ORG_CREATED = "org_created"
    ORG_UPDATED = "org_updated"
    ORG_DELETED = "org_deleted"
    MEMBER_INVITED = "member_invited"
    MEMBER_ROLE_CHANGED = "member_role_changed"
    MEMBER_REMOVED = "member_removed"
    MEMBER_LEFT = "member_left"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that looks like a credential, recursively."""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
            cleaned[key] = REDACTED
        elif isinstance(value, dict):
            cleaned[key] = redact(value)
        elif isinstance(value, list):
            cleaned[key] = [redact(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


def pseudonymise_email(email: str) -> str:
    """Keyed, truncated HMAC of an email address.

    Deterministic, so all attempts against one address group together; keyed,
    so the audit table cannot be brute-forced back into an address list by
    anyone who does not also hold SECRET_KEY.
    """
    return sign(email.strip().lower(), purpose="audit-email")[:32]


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    outcome: Outcome,
    user_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """Write one audit row.

    Never raises: an audit failure must not take down the request it describes.
    The structured log line is the fallback record if the insert fails.
    """
    settings = get_settings()
    if not settings.audit_log_enabled:
        return

    payload = redact(detail or {})
    serialised = json.dumps(payload, default=str, separators=(",", ":"))
    if len(serialised) > MAX_DETAIL_BYTES:
        serialised = json.dumps({"truncated": True, "size": len(serialised)})

    logger.info(
        "audit",
        action=action.value,
        outcome=outcome.value,
        user_id=str(user_id) if user_id else None,
        ip_address=ip_address,
        **payload,
    )

    try:
        session.add(
            AuditEvent(
                created_at=datetime.now(UTC),
                user_id=user_id,
                action=action.value,
                outcome=outcome.value,
                ip_address=(ip_address or "")[:45] or None,
                user_agent=(user_agent or "")[:512] or None,
                detail=serialised,
            )
        )
        await session.flush()
        if commit:
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - audit must never break the request
        logger.error("audit_write_failed", action=action.value, error=str(exc))
