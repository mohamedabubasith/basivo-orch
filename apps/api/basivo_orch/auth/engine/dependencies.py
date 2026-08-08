"""User dependencies exposed to the rest of the application.

Everything outside ``app.auth.engine`` injects these rather than building its
own, so the authority model lives in one file.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi_users import FastAPIUsers

from basivo_orch.auth.engine.backends import auth_backends
from basivo_orch.auth.engine.manager import get_user_manager
from basivo_orch.auth.models import User

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, auth_backends)

#: Authenticated, ``is_active`` — the normal requirement for a protected route.
current_active_user = fastapi_users.current_user(active=True)

#: Authenticated, active **and** ``is_verified``. Use for anything that can
#: send mail, spend money or change security settings: an unverified account
#: may belong to someone who does not control the address.
current_verified_user = fastapi_users.current_user(active=True, verified=True)

#: Administrative authority.
current_superuser = fastapi_users.current_user(active=True, superuser=True)

#: Returns ``None`` instead of 401 when unauthenticated. For routes whose
#: response varies by login state without requiring it.
current_optional_user = fastapi_users.current_user(active=True, optional=True)


async def current_fresh_user(user: User = Depends(current_active_user)) -> User:
    """Require a session that was authenticated recently.

    Step-up gate for destructive operations — changing an email address,
    disabling 2FA, deleting the account. A stolen laptop with a live session
    should not be enough to take permanent control of an account.
    """
    from datetime import UTC, datetime, timedelta

    freshness_window = timedelta(minutes=15)
    if user.last_login_at is None or datetime.now(UTC) - user.last_login_at > freshness_window:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please re-authenticate to perform this action.",
            headers={"X-Reauthentication-Required": "true"},
        )
    return user
