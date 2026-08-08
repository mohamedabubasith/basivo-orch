"""Persistence model for authentication.

Storage rules enforced here:

* No secret is ever stored in a form that is useful if the database leaks.
  Passwords are Argon2id hashes; refresh tokens, reset tokens and recovery codes
  are SHA-256 digests; TOTP seeds are encrypted at rest.
* Refresh tokens carry a ``family_id``. Rotation issues a new token in the same
  family; presenting an already-rotated token proves the token was stolen, and
  the whole family is revoked. See ``app.auth.security.tokens``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from basivo_orch.auth.db import Base, TimestampMixin, UUIDMixin
from basivo_orch.auth.engine.types import OAuthAccountTableBase, UserTableBase

if TYPE_CHECKING:  # pragma: no cover
    pass


class User(UserTableBase, TimestampMixin, Base):
    """The user account.

    ``UserTableBase`` supplies ``id`` (UUID primary key), ``email``
    (unique, indexed, 320 chars — the RFC 5321 maximum), ``hashed_password``,
    ``is_active``, ``is_superuser`` and ``is_verified``. Everything below is
    this service's own. Emails are stored lower-cased — see
    ``app.auth.engine.adapters.UserDatabase``.
    """

    __tablename__ = "user"

    if TYPE_CHECKING:  # pragma: no cover
        # UserTableBase declares these as plain types (`email: str`) so the model
        # satisfies the engine's UserProtocol. The side effect is that
        # `User.email == x` looks like a bool comparison to mypy rather than a
        # SQLAlchemy expression, which breaks every query that filters on them.
        # Re-declaring as Mapped[...] restores that typing. Runtime is untouched:
        # the base class's real columns are what SQLAlchemy maps.
        id: Mapped[uuid.UUID]
        email: Mapped[str]
        hashed_password: Mapped[str]
        is_active: Mapped[bool]
        is_superuser: Mapped[bool]
        is_verified: Mapped[bool]

    # -- Credential lifecycle ---------------------------------------------
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Tokens minted before this instant are rejected, so a password change
    logs out every other session without needing to enumerate them."""

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(45))  # INET6 max length

    # -- Lockout -----------------------------------------------------------
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- TOTP --------------------------------------------------------------
    totp_secret: Mapped[str | None] = mapped_column(Text)
    """Fernet-encrypted base32 seed. Never the raw seed: a database leak would
    otherwise hand the attacker a working second factor."""

    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_last_counter: Mapped[int | None] = mapped_column(Integer)
    """Highest accepted time-step. Blocks replay of a code inside its own window."""

    # -- Relationships -----------------------------------------------------
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    recovery_codes: Mapped[list[RecoveryCode]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        # Membership has two foreign keys to `user` (the member, and whoever
        # invited them). Without naming one, SQLAlchemy cannot infer the join.
        foreign_keys="Membership.user_id",
    )

    def __repr__(self) -> str:  # pragma: no cover
        # Deliberately does not include the email: repr() ends up in logs and
        # tracebacks, and that is how PII leaks into observability pipelines.
        return f"<User {self.id}>"


class RefreshToken(UUIDMixin, TimestampMixin, Base):
    """One issued refresh token.

    Rows are never deleted on rotation — only marked used. A used row that is
    presented again is the signal that a token was exfiltrated.
    """

    __tablename__ = "refresh_token"
    __table_args__ = (
        Index("ix_refresh_token_family", "family_id"),
        Index("ix_refresh_token_user_active", "user_id", "revoked_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    """SHA-256 of the opaque token. The token itself is never persisted."""

    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False, default=uuid.uuid4)
    """Shared by every token descended from one login. Revoked as a unit."""

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class AuditEvent(UUIDMixin, Base):
    """Append-only security event log.

    No ``updated_at`` and no update path: audit rows are written once. Keeping
    them in the primary database makes them transactional with the action they
    describe; ship them to durable storage separately for retention.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_user_created", "user_id", "created_at"),
        Index("ix_audit_event_action_created", "action", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    """Nullable: failed logins for non-existent accounts still get logged."""

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    detail: Mapped[str | None] = mapped_column(Text)
    """JSON blob. Redacted before write — see app.auth.security.audit."""


class OAuthAccount(OAuthAccountTableBase, TimestampMixin, Base):
    """A third-party identity linked to a local user.

    ``OAuthAccountTableBase`` supplies ``id``, ``user_id``, ``oauth_name``,
    ``account_id``, ``account_email``, ``access_token``, ``refresh_token`` and
    ``expires_at``.
    """

    __tablename__ = "oauth_account"
    __table_args__ = (
        # One provider account maps to at most one local user. Without this a
        # single Google identity could be linked to several accounts, and which
        # one a login resolves to becomes non-deterministic.
        UniqueConstraint("oauth_name", "account_id", name="uq_oauth_account_provider_account"),
    )

    user: Mapped[User] = relationship(back_populates="oauth_accounts")


class RecoveryCode(UUIDMixin, Base):
    """Single-use 2FA recovery code, stored hashed."""

    __tablename__ = "recovery_code"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="recovery_codes")


class Organization(UUIDMixin, TimestampMixin, Base):
    """A tenant. Every org-scoped query must be filtered by this row's id."""

    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    """Deactivating hides the org from every member — `load_context` treats an
    inactive org as non-existent, which suspends access without deleting data."""

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class Membership(UUIDMixin, TimestampMixin, Base):
    """A user's role inside one organisation.

    This row *is* the authorization decision. It is re-read on every org-scoped
    request rather than cached in the access token, so a demotion takes effect
    immediately instead of at the next token expiry.
    """

    __tablename__ = "membership"
    __table_args__ = (
        # One membership per (user, org). Without this a user could hold two
        # rows with different roles and which one wins would be arbitrary.
        UniqueConstraint("user_id", "organization_id", name="uq_membership_user_organization"),
        # The hot path: resolving a caller's role for a given org on every request.
        Index("ix_membership_org_user", "organization_id", "user_id"),
        # Supports the last-owner check without scanning the table.
        Index("ix_membership_org_role", "organization_id", "role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    """Stored as text, validated against `app.auth.authz.Role` on read.

    Deliberately not a database enum: adding a role would otherwise need a
    migration with a table lock on Postgres. `load_context` fails closed on any
    value it does not recognise, so an unknown string grants nothing."""

    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    """Who granted this membership. The audit trail for how someone got access."""

    user: Mapped[User] = relationship(back_populates="memberships", foreign_keys=[user_id])
    organization: Mapped[Organization] = relationship(back_populates="memberships")
