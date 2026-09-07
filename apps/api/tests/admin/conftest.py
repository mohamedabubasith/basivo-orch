"""Fixtures for the platform admin suite."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-" + "a" * 40)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from basivo_orch.auth.models import Organization, User
from basivo_orch.billing import pricing
from basivo_orch.config import get_settings
from basivo_orch.db import Base

import basivo_orch.models  # noqa: F401,E402  isort:skip


@pytest.fixture(autouse=True)
def _billing_is_live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_MODE", "production")
    monkeypatch.setenv("DODO_API_KEY", "dodo-test-key")
    monkeypatch.setenv("DODO_WEBHOOK_SECRET", "whsec_dGVzdC1zZWNyZXQ=")
    monkeypatch.setenv("DODO_PRODUCT_PRO", "pdt_pro")
    monkeypatch.setenv("DODO_PRODUCT_TEAM", "pdt_team")
    get_settings.cache_clear()
    pricing.invalidate()
    yield
    get_settings.cache_clear()
    pricing.invalidate()


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.fixture
async def organization(session: AsyncSession) -> Organization:
    org = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


@pytest.fixture
async def staff(session: AsyncSession) -> User:
    user = User(
        email="staff@basivo.in",
        hashed_password="x",  # noqa: S106 - never verified here
        is_active=True,
        is_verified=True,
        is_superuser=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def customer(session: AsyncSession) -> User:
    user = User(
        email="customer@example.com",
        hashed_password="x",  # noqa: S106 - never verified here
        is_active=True,
        is_verified=True,
        is_superuser=False,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


class FakeRequest:
    """A Request with only what the dependency reads."""

    def __init__(self, path: str = "/api/v1/admin/overview") -> None:
        self.url = type("Url", (), {"path": path})()
        self.headers: dict[str, str] = {}
        self.client = type("Client", (), {"host": "127.0.0.1"})()
        self.scope: dict = {"headers": []}
