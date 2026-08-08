"""The auth engine seam.

**This package is the only place in the codebase allowed to import
``fastapi_users``.** The rule is enforced by ruff (`TID251`, configured in
``pyproject.toml``) and by a dedicated CI job, not by convention, so a violation
fails the build.

Why the indirection exists
--------------------------
``fastapi-users`` entered maintenance mode in March 2026: it still receives
security and dependency updates, but no new features, and its authors are
building a successor toolkit. It remains the right base — it is MIT, widely
deployed and battle-tested, and the alternative is hand-writing several thousand
lines of security-critical flow logic with no external review.

The risk it does carry is a future migration. Confining every import to this
package bounds that migration to roughly four hundred lines behind a stable
interface. Routers, models, tests and business logic never move.

Anything outside this package imports from here::

    from basivo_orch.auth.engine import current_active_user, get_user_manager

Why the re-exports are lazy
---------------------------
``app.auth.models`` needs the ORM base tables from :mod:`app.auth.engine.types`.
Importing any submodule runs this ``__init__`` first, and if that eagerly
imported :mod:`~app.auth.engine.adapters` — which imports ``app.auth.models`` —
the two modules would deadlock on each other at import time.

Resolving the names in ``__getattr__`` (PEP 562) instead means
``app.auth.engine.types`` can be imported on its own without dragging in the
manager, the backends or the ORM. The public API is unchanged; the imports
simply happen on first attribute access rather than at module load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - for type checkers and IDEs only
    from basivo_orch.auth.engine.adapters import get_user_db
    from basivo_orch.auth.engine.backends import (
        auth_backends,
        cookie_backend,
        get_jwt_strategy,
    )
    from basivo_orch.auth.engine.dependencies import (
        current_active_user,
        current_fresh_user,
        current_optional_user,
        current_superuser,
        current_verified_user,
        fastapi_users,
    )
    from basivo_orch.auth.engine.manager import UserManager, get_user_manager
    from basivo_orch.auth.engine.oauth import oauth_clients

#: Public name -> submodule that defines it.
_EXPORTS: dict[str, str] = {
    "UserManager": "manager",
    "auth_backends": "backends",
    "cookie_backend": "backends",
    "current_active_user": "dependencies",
    "current_fresh_user": "dependencies",
    "current_optional_user": "dependencies",
    "current_superuser": "dependencies",
    "current_verified_user": "dependencies",
    "fastapi_users": "dependencies",
    "get_jwt_strategy": "backends",
    "get_user_db": "adapters",
    "get_user_manager": "manager",
    "oauth_clients": "oauth",
}

# Spelled out rather than derived from _EXPORTS. Under `mypy --strict`,
# `no_implicit_reexport` only recognises a literal __all__; a computed one leaves
# every consumer failing with "does not explicitly export attribute".
__all__ = [
    "UserManager",
    "auth_backends",
    "cookie_backend",
    "current_active_user",
    "current_fresh_user",
    "current_optional_user",
    "current_superuser",
    "current_verified_user",
    "fastapi_users",
    "get_jwt_strategy",
    "get_user_db",
    "get_user_manager",
    "oauth_clients",
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    module = import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    # Cache on the package so subsequent lookups skip __getattr__ entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
