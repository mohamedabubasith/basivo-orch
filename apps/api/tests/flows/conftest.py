"""Fixtures for the flow engine tests.

Runs on SQLite in-memory with no Redis, so `pytest` needs no containers. That
is also a real code path — a deployment without Redis falls back to polling —
so exercising it here is not only a convenience.
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

from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from basivo_orch.auth.models import Organization
from basivo_orch.db import Base
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Flow, FlowVersion, Run, RunStatus, TriggerKind

# Importing the models package registers every table on Base.metadata.
import basivo_orch.models  # noqa: F401,E402  isort:skip


@pytest.fixture
async def sessions() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """A sessionmaker bound to one in-memory database.

    Exposed separately from `session` because code that opens its own sessions
    — the worker does, so a heartbeat is not stuck behind a node's
    transaction — takes a factory. Without this, such code would silently talk
    to the app's real sessionmaker and find no tables at all.

    `StaticPool` keeps every session on the *same* in-memory database; the
    default pool hands out fresh connections, and each new connection to
    `:memory:` is a brand new empty database.
    """
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
def make_run(session: AsyncSession, organization: Organization):
    """Build a persisted flow + version + queued run from a graph."""

    async def _make(graph: Graph, payload: dict | None = None) -> Run:
        flow = Flow(
            organization_id=organization.id,
            name="Test flow",
            slug=f"test-{uuid.uuid4().hex[:8]}",
        )
        session.add(flow)
        await session.flush()

        version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
        session.add(version)
        await session.flush()

        run = Run(
            flow_id=flow.id,
            flow_version_id=version.id,
            organization_id=organization.id,
            trigger=TriggerKind.MANUAL,
            input={"payload": payload or {}},
            status=RunStatus.QUEUED,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    return _make
