"""Database adapter wiring for the auth engine."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.models import OAuthAccount, User


class UserDatabase(SQLAlchemyUserDatabase[User, uuid.UUID]):
    """Adapter over the ``User`` table.

    Subclassed rather than used directly so email normalisation lives in one
    place. Postgres string comparison is case-sensitive, so without this
    ``Ada@example.com`` and ``ada@example.com`` become two accounts — and the
    second one silently bypasses whatever the first one's lockout state was.
    """

    async def get_by_email(self, email: str) -> User | None:
        return await super().get_by_email(email.strip().lower())


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[UserDatabase, None]:
    yield UserDatabase(session, User, OAuthAccount)
