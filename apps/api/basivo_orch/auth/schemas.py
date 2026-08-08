"""Request and response schemas.

Read schemas are explicit allowlists. Returning ORM objects directly is how
``hashed_password``, ``totp_secret`` and lockout counters end up in an API
response after someone adds a column months later.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from basivo_orch.auth.engine.types import BaseUser, BaseUserCreate, BaseUserUpdate
from basivo_orch.auth.settings import get_settings


class UserRead(BaseUser[uuid.UUID]):
    """Safe projection of a user."""

    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
    last_login_at: datetime | None = None
    totp_enabled: bool = False


class UserCreate(BaseUserCreate):
    @field_validator("email", mode="after")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseUserUpdate):
    @field_validator("email", mode="after")
    @classmethod
    def _normalise_email(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else value


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - a scheme name, not a credential
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Omit when using the cookie transport; the cookie is read instead.",
    )


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=1024)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email", mode="after")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=1024)


class MessageResponse(BaseModel):
    """Deliberately uninformative success envelope.

    Used by every flow keyed on an email address. Returning the same message
    whether or not the account exists is what prevents these endpoints from
    becoming a user-enumeration oracle.
    """

    detail: str


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------


class OTPRequestPayload(BaseModel):
    email: EmailStr

    @field_validator("email", mode="after")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class OTPVerifyPayload(BaseModel):
    email: EmailStr
    code: str

    @field_validator("email", mode="after")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("code", mode="after")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        cleaned = value.strip().replace(" ", "").replace("-", "")
        settings = get_settings()
        if not cleaned.isdigit() or len(cleaned) != settings.otp_length:
            # Shape is rejected before any store lookup, so malformed input
            # costs nothing and cannot be used to probe timing.
            raise ValueError("Invalid code.")
        return cleaned


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


class TOTPEnrolStart(BaseModel):
    secret: str = Field(description="Base32 seed. Shown once, at enrolment.")
    provisioning_uri: str = Field(description="otpauth:// URI for authenticator apps.")
    qr_code_svg: str = Field(description="Inline SVG rendering of the URI.")


class TOTPConfirmPayload(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TOTPEnrolComplete(BaseModel):
    recovery_codes: list[str] = Field(
        description="Single-use codes. Displayed once and never retrievable again — "
        "only their hashes are stored."
    )


class StepUpChallenge(BaseModel):
    """Returned by login when a second factor is outstanding."""

    step_up_token: str = Field(description="Exchange at /auth/2fa/verify. Not an access token.")
    methods: list[str]
    expires_in: int


class TOTPVerifyPayload(BaseModel):
    step_up_token: str
    code: str = Field(min_length=6, max_length=32)


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------


class SSOAuthorizeResponse(BaseModel):
    authorization_url: str


class SSOProvider(BaseModel):
    name: str
    display_name: str
    authorize_path: str


# ---------------------------------------------------------------------------
# Organisations and membership
# ---------------------------------------------------------------------------

#: Slugs that would collide with an API path segment, or read as first-party if
#: the frontend routes tenants by subdomain.
RESERVED_ORG_SLUGS = frozenset(
    {
        "admin", "api", "app", "auth", "billing", "docs", "health", "help",
        "internal", "login", "mail", "new", "orgs", "root", "settings",
        "signup", "static", "status", "support", "system", "users", "www",
    }
)



class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=128)

    @field_validator("slug", mode="after")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", cleaned):
            raise ValueError("Use lowercase letters, digits and single hyphens.")
        if cleaned in RESERVED_ORG_SLUGS:
            # These would collide with API paths or be mistaken for first-party
            # subdomains in a slug-routed frontend.
            raise ValueError(f"{cleaned!r} is reserved.")
        return cleaned


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    created_at: datetime


class OrganizationSummary(OrganizationRead):
    """An organisation plus the caller's own standing in it."""

    role: str
    permissions: list[str]


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: EmailStr
    role: str
    created_at: datetime
    invited_by_id: uuid.UUID | None = None


class MemberInvite(BaseModel):
    email: EmailStr
    role: str = "member"

    @field_validator("email", mode="after")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class MemberRoleUpdate(BaseModel):
    role: str


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    redis: str
