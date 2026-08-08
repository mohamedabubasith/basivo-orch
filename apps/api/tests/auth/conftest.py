"""Test fixtures.

The suite runs against SQLite in-memory and a fake Redis by default, so
``pytest`` needs no containers and CI stays fast. Tests that must exercise real
Postgres or Redis behaviour are marked ``slow`` and opt into testcontainers.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator

import pytest

# Set before any app module is imported: Settings is instantiated at import time
# in several modules, and it refuses to construct without these.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-" + "a" * 40)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("PASSWORD_CHECK_BREACHES", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import httpx
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from basivo_orch.auth.db import Base, get_async_session
from basivo_orch.auth.models import User
from basivo_orch.auth.security.passwords import hash_password
from basivo_orch.auth.settings import get_settings

TEST_PASSWORD = "correct-horse-battery-staple-42"


@pytest.fixture
def password() -> str:
    """The known-good password, as a fixture.

    A fixture rather than a cross-module import: the tests package sits at a
    different path in each install mode, so `from tests.conftest import ...`
    would only resolve in one of them.
    """
    return TEST_PASSWORD


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def engine() -> AsyncGenerator:
    """A fresh in-memory database per test.

    StaticPool keeps every connection pointed at the same in-memory database;
    without it each connection would get its own empty one.
    """
    from sqlalchemy.pool import StaticPool

    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as test_session:
        yield test_session


@pytest.fixture
async def redis() -> AsyncGenerator:
    """Fake Redis, flushed between tests so state never leaks across them."""
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
async def state(redis):
    """The ephemeral-state handle, whichever backend was generated.

    Tests take `state` rather than `redis` so the same test body works for both
    builds; only this fixture differs.
    """
    return redis


def build_app():
    """The application under test.
    Embedded mode has no application of its own, so this assembles the same
    thing your project does: a bare FastAPI app with the auth middleware and
    router installed. Testing through it means these tests exercise the real
    middleware stack, not a shortcut around it.
    """
    from fastapi import FastAPI

    from basivo_orch.auth.router import auth_router, install_auth

    application = FastAPI()
    install_auth(application)
    application.include_router(auth_router)
    return application


@pytest.fixture
async def app(engine, monkeypatch, redis):
    from basivo_orch.auth.security import redis_client

    monkeypatch.setattr(redis_client, "get_redis", lambda: redis)

    application = build_app()

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as test_session:
            yield test_session

    application.dependency_overrides[get_async_session] = override_session
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture
async def user(session: AsyncSession) -> User:
    record = User(
        id=uuid.uuid4(),
        email="ada@example.com",
        hashed_password=hash_password(TEST_PASSWORD),
        is_active=True,
        is_verified=True,
    )
    session.add(record)
    await session.commit()
    return record


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Settings is lru_cached; a test that patches env must not leak into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
