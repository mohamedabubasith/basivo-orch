"""Engine types re-exported for the rest of the application.

Schemas and routers need the library's base schema classes and exception types.
Importing them directly would punch a hole straight through the seam this
package exists to maintain, so they are re-exported here instead.

Deliberately light: this module imports only from ``fastapi_users`` and pulls in
none of the engine's wiring, so ``app.auth.schemas`` can import it without a
circular dependency on the manager.

If the engine is ever swapped out, this file is the compatibility shim — map the
new library's equivalents onto these names and the rest of the codebase does not
move.
"""

from __future__ import annotations

from fastapi_users import schemas as _schemas
from fastapi_users.exceptions import (
    FastAPIUsersException,
    InvalidID,
    InvalidPasswordException,
    InvalidResetPasswordToken,
    InvalidVerifyToken,
    UserAlreadyExists,
    UserAlreadyVerified,
    UserInactive,
    UserNotExists,
)
from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseOAuthAccountTableUUID,
    SQLAlchemyBaseUserTableUUID,
)

# Base schema classes
BaseUser = _schemas.BaseUser
BaseUserCreate = _schemas.BaseUserCreate
BaseUserUpdate = _schemas.BaseUserUpdate
BaseOAuthAccount = _schemas.BaseOAuthAccount
BaseOAuthAccountMixin = _schemas.BaseOAuthAccountMixin

# Base ORM tables.
#
# `app.auth.models` inherits these rather than hand-rolling the id / email /
# hashed_password / is_active / is_superuser / is_verified columns. Two reasons:
# the column definitions then cannot drift from what the engine expects, and the
# bases declare plain (non-`Mapped`) types under TYPE_CHECKING, which is what
# lets the model satisfy the engine's `UserProtocol` under mypy --strict.
#
# This is the one place the persistence layer touches the engine. An engine
# migration replaces these two names with equivalents (or with hand-written
# columns) and `app.auth.models` keeps its own extra columns unchanged.
UserTableBase = SQLAlchemyBaseUserTableUUID
OAuthAccountTableBase = SQLAlchemyBaseOAuthAccountTableUUID

__all__ = [
    "BaseOAuthAccount",
    "BaseOAuthAccountMixin",
    "BaseUser",
    "BaseUserCreate",
    "BaseUserUpdate",
    "FastAPIUsersException",
    "InvalidID",
    "InvalidPasswordException",
    "InvalidResetPasswordToken",
    "InvalidVerifyToken",
    "OAuthAccountTableBase",
    "UserAlreadyExists",
    "UserAlreadyVerified",
    "UserInactive",
    "UserNotExists",
    "UserTableBase",
]
