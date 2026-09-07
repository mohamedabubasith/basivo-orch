"""Fixtures for the billing suite.

Same shape as the flow suite's: SQLite in memory, no containers, no provider.
Nothing here talks to Dodo — the outbound calls are the two functions in
`billing.provider`, and the tests that need them replace those.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

# Must precede any app import: several modules build Settings at import time.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-" + "a" * 40)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.billing import pricing
from basivo_orch.config import get_settings
from basivo_orch.db import Base

# Importing the models package registers every table on Base.metadata.
import basivo_orch.models  # noqa: F401,E402  isort:skip


@pytest.fixture(autouse=True)
def _billing_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most of this suite is about what happens when money is real.

    Production mode is validated at construction, so the fake provider
    settings have to be here rather than only in the tests that call out.
    """
    monkeypatch.setenv("BILLING_MODE", "production")
    monkeypatch.setenv("DODO_API_KEY", "dodo-test-key")
    monkeypatch.setenv("DODO_WEBHOOK_SECRET", "whsec_dGVzdC1zZWNyZXQ=")
    monkeypatch.setenv("DODO_PRODUCT_PRO", "pdt_pro")
    monkeypatch.setenv("DODO_PRODUCT_TEAM", "pdt_team")
    get_settings.cache_clear()
    # The merged plan catalogue is cached in the process; each test gets its
    # own database, so a cached override would leak into the next one.
    pricing.invalidate()
    yield
    get_settings.cache_clear()
    pricing.invalidate()


@pytest.fixture
def demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Billing switched off, which is the default a deployment ships with."""
    monkeypatch.setenv("BILLING_MODE", "demo")
    get_settings.cache_clear()


@pytest.fixture
async def sessions() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with sessions() as db:
        yield db


@pytest.fixture
async def organization(session: AsyncSession) -> Organization:
    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


@pytest.fixture
async def other_organization(session: AsyncSession) -> Organization:
    org = Organization(name="Rival", slug=f"rival-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


def make_context(organization: Organization, role: Role = Role.OWNER) -> OrgContext:
    from basivo_orch.auth.authz import ROLE_PERMISSIONS

    user = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        hashed_password="x",  # noqa: S106 - never verified; auth ran before this
        is_active=True,
    )
    return OrgContext(
        user=user,
        organization=organization,
        role=role,
        permissions=ROLE_PERMISSIONS[role],
    )


ALL: frozenset[Permission] = frozenset(Permission)
