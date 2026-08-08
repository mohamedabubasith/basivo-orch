"""Database seam for embedded mode.

This module exists so the rest of the auth package never has to know how the
host project wires SQLAlchemy. It re-exports **your** declarative base and
**your** session dependency, which means:

* auth tables live in your database, on your metadata — so your existing
  ``alembic revision --autogenerate`` picks them up with no extra configuration
* auth shares your connection pool rather than opening a second one
* your own tables can carry a real foreign key to ``user``
* a request that touches both your data and auth data does so in one
  transaction, so a partial failure rolls back cleanly

If you move your Base or rename the session dependency, this is the only file
that needs to change.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

# The host project's declarative base and session dependency.
from basivo_orch.db import Base as Base
from basivo_orch.db import get_async_session as get_async_session

__all__ = ["Base", "TimestampMixin", "UUIDMixin", "get_async_session"]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
