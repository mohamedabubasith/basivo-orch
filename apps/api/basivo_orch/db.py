"""The single database layer for the whole service.

One engine, one connection pool, one declarative ``Base``, one migration
history. The auth package installed under ``basivo_orch/auth`` imports ``Base``
and ``get_async_session`` from here rather than building its own, which is what
lets orchestrator tables foreign-key to ``user`` and ``organisation`` later.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from basivo_orch.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every table in the service, auth included."""


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    # Pool sizing only applies to server-backed drivers; SQLite (used by tests)
    # rejects these arguments outright.
    kwargs: dict[str, object] = {
        "echo": settings.DATABASE_ECHO,
        "pool_pre_ping": True,
        "future": True,
    }
    if not settings.DATABASE_URL.startswith("sqlite"):
        kwargs |= {
            "pool_size": settings.DATABASE_POOL_SIZE,
            "max_overflow": settings.DATABASE_MAX_OVERFLOW,
            "pool_recycle": settings.DATABASE_POOL_RECYCLE_SECONDS,
        }
    return create_async_engine(settings.DATABASE_URL, **kwargs)


engine: AsyncEngine = _create_engine()

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back on an unhandled exception so a failed request can never leave a
    half-applied transaction behind for the next borrower of the connection.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close pooled connections on shutdown."""
    await engine.dispose()
