"""Who counts as platform staff.

One dependency, used by every admin route. Staff is `is_superuser` on the user
row, which is set with `manage.py`, never from the product: a screen that can
promote its own caller is the whole security model gone.

A caller who is not staff gets 404, not 403. A 403 confirms the endpoint
exists, and the existence of an admin API that lists every workspace is not
something to confirm to whoever asks.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from basivo_orch.auth.models import User
from basivo_orch.auth.security.ratelimit import client_ip
from basivo_orch.gate import current_app_user
from basivo_orch.logging import get_logger

log = get_logger(__name__)

NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


async def require_platform_admin(request: Request, user: User = Depends(current_app_user)) -> User:
    if not user.is_superuser:
        log.warning(
            "admin.denied",
            user_id=str(user.id),
            ip=client_ip(request),
            path=request.url.path,
        )
        raise NOT_FOUND
    # Logged on every call on purpose: this is the account that can read every
    # workspace's failures, and an audit trail of that is not optional.
    log.info("admin.access", user_id=str(user.id), path=request.url.path)
    return user
