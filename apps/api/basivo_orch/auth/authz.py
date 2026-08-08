"""Authorization: per-organisation roles and permissions.

Authentication answers *who you are*. This module answers *what you may do*, and
it is scoped per organisation: a user is an owner of one org and a viewer of
another, with no authority carried between them.

Three rules hold everything together:

1. **Routes name permissions, never roles.** ``Depends(require(Permission.MEMBER_REMOVE))``
   rather than ``require_role(Role.ADMIN)``. Roles are named bundles of
   permissions, so moving a capability between roles is a one-line change in
   :data:`ROLE_PERMISSIONS` instead of an audit of every route.

2. **Authority is always resolved from the database, never from the token.**
   Access tokens live for 15 minutes. If a role were baked into the token, a
   demoted user would keep their old authority until it expired. Membership is
   re-read per request.

3. **Not-a-member is reported as 404, not 403.** A 403 confirms the
   organisation exists, which lets an attacker enumerate org IDs and map your
   customer list. Non-existent and not-visible are deliberately indistinguishable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.engine import current_active_user
from basivo_orch.auth.models import Membership, Organization, User
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.ratelimit import client_ip
from basivo_orch.auth.settings import get_settings

logger = structlog.get_logger(__name__)


class Role(StrEnum):
    """A user's role *within one organisation*.

    Ordering matters for the escalation guards below, so each role carries an
    explicit rank rather than relying on declaration order.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def outranks(self, other: Role) -> bool:
        return self.rank > other.rank

    def can_grant(self, target: Role) -> bool:
        """Whether a holder of this role may assign ``target`` to someone.

        You may grant any role at or below your own. An admin can therefore
        appoint another admin, but only an owner can create an owner — without
        that ceiling, any admin could promote themselves past every check in
        this module.
        """
        return self.rank >= target.rank


_ROLE_RANK: dict[Role, int] = {
    Role.OWNER: 3,
    Role.ADMIN: 2,
    Role.MEMBER: 1,
    Role.VIEWER: 0,
}


class Permission(StrEnum):
    """A single capability.

    This is the extension point. Add your product's permissions here and grant
    them in :data:`ROLE_PERMISSIONS`; route code then never needs to know which
    roles exist.
    """

    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"
    ORG_TRANSFER = "org:transfer"

    MEMBER_READ = "member:read"
    MEMBER_INVITE = "member:invite"
    MEMBER_ROLE_UPDATE = "member:role_update"
    MEMBER_REMOVE = "member:remove"

    AUDIT_READ = "audit:read"

    # --- orchestrator -----------------------------------------------------
    # Added by this product, at the extension point above. Kept in one block so
    # `basivo-auth update` has a clean region to merge around.
    FLOW_READ = "flow:read"
    FLOW_CREATE = "flow:create"
    FLOW_UPDATE = "flow:update"
    FLOW_DELETE = "flow:delete"
    FLOW_PUBLISH = "flow:publish"
    FLOW_RUN = "flow:run"

    RUN_READ = "run:read"
    RUN_CANCEL = "run:cancel"

    APIKEY_READ = "apikey:read"
    APIKEY_CREATE = "apikey:create"
    APIKEY_REVOKE = "apikey:revoke"


#: Which permissions each role carries. The single source of truth for authority.
#:
#: Deliberately *not* inherited by rank: an explicit set per role means you can
#: read one line and know exactly what a role can do, and a permission cannot be
#: granted by accident through a hierarchy someone changed elsewhere.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(
        {
            Permission.ORG_READ,
            # Read-only staff can watch runs. Seeing that a flow failed is not
            # the same authority as being able to start one.
            Permission.FLOW_READ,
            Permission.RUN_READ,
        }
    ),
    Role.MEMBER: frozenset(
        {
            Permission.ORG_READ,
            Permission.MEMBER_READ,
            Permission.FLOW_READ,
            Permission.FLOW_CREATE,
            Permission.FLOW_UPDATE,
            Permission.FLOW_RUN,
            Permission.RUN_READ,
            Permission.RUN_CANCEL,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.ORG_READ,
            Permission.ORG_UPDATE,
            Permission.MEMBER_READ,
            Permission.MEMBER_INVITE,
            Permission.MEMBER_ROLE_UPDATE,
            Permission.MEMBER_REMOVE,
            Permission.AUDIT_READ,
            Permission.FLOW_READ,
            Permission.FLOW_CREATE,
            Permission.FLOW_UPDATE,
            Permission.FLOW_RUN,
            Permission.RUN_READ,
            Permission.RUN_CANCEL,
            # Publishing exposes a flow to the outside world over an API key,
            # and deleting destroys its run history. Both are admin-level.
            Permission.FLOW_DELETE,
            Permission.FLOW_PUBLISH,
            Permission.APIKEY_READ,
            Permission.APIKEY_CREATE,
            Permission.APIKEY_REVOKE,
        }
    ),
    Role.OWNER: frozenset(Permission),
}


@dataclass(frozen=True, slots=True)
class OrgContext:
    """The authenticated user *as a member of one organisation*.

    Injected into every org-scoped route. Carry ``organization_id`` into every
    query the route makes — that is what enforces tenant isolation.
    """

    user: User
    organization: Organization
    role: Role
    permissions: frozenset[Permission]
    via_superuser: bool = False
    """True when access came from the platform break-glass path, not membership."""

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Assert a permission mid-handler, for checks that depend on the body."""
        if not self.has(permission):
            raise _forbidden(permission)


def _article(role: Role) -> str:
    """ "a viewer" but "an admin" — these strings are shown to end users."""
    return "an" if role.value[0] in "aeiou" else "a"


def _forbidden(permission: Permission) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"This action requires the {permission.value} permission.",
    )


def _not_found() -> HTTPException:
    """Used for both 'no such organisation' and 'you are not a member'.

    Keeping them identical is what stops org IDs being enumerable.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Organisation not found.",
    )


async def load_context(
    session: AsyncSession,
    *,
    user: User,
    organization_id: uuid.UUID,
) -> OrgContext:
    """Resolve a user's authority within one organisation.

    Raises 404 when the organisation does not exist, is inactive, or the user is
    not a member of it — all three are indistinguishable to the caller.
    """
    settings = get_settings()

    result = await session.execute(
        select(Organization, Membership)
        .outerjoin(
            Membership,
            (Membership.organization_id == Organization.id) & (Membership.user_id == user.id),
        )
        .where(Organization.id == organization_id)
    )
    row = result.first()
    if row is None:
        raise _not_found()

    organization, membership = row
    if not organization.is_active:
        raise _not_found()

    if membership is None:
        # Break-glass for platform staff. Off by default: a global flag that
        # silently grants access to every tenant is exactly the kind of
        # authority that gets forgotten about. Every use is audited.
        if user.is_superuser and settings.superuser_bypasses_org_permissions:
            logger.warning(
                "superuser_org_access",
                user_id=str(user.id),
                organization_id=str(organization_id),
            )
            return OrgContext(
                user=user,
                organization=organization,
                role=Role.OWNER,
                permissions=ROLE_PERMISSIONS[Role.OWNER],
                via_superuser=True,
            )
        raise _not_found()

    try:
        role = Role(membership.role)
    except ValueError:
        # An unrecognised role string means the database holds a value this
        # build does not know about (a downgrade, or a manual edit). Fail
        # closed: grant nothing rather than guess.
        logger.error(
            "unknown_role",
            role=membership.role,
            user_id=str(user.id),
            organization_id=str(organization_id),
        )
        raise _not_found() from None

    return OrgContext(
        user=user,
        organization=organization,
        role=role,
        permissions=ROLE_PERMISSIONS[role],
    )


OrgDependency = Callable[..., Coroutine[Any, Any, OrgContext]]


def require(*permissions: Permission) -> OrgDependency:
    """Build a dependency requiring **all** of ``permissions`` in the path's org.

    Usage::

        @router.delete("/orgs/{organization_id}/members/{user_id}")
        async def remove_member(
            context: OrgContext = Depends(require(Permission.MEMBER_REMOVE)),
        ) -> None: ...

    The organisation is taken from the ``organization_id`` path parameter, so
    the authority check and the resource being addressed can never disagree.
    """
    required = frozenset(permissions)

    async def dependency(
        request: Request,
        organization_id: uuid.UUID = Path(description="Organisation the request targets."),
        user: User = Depends(current_active_user),
        session: AsyncSession = Depends(get_async_session),
    ) -> OrgContext:
        context = await load_context(session, user=user, organization_id=organization_id)

        missing = required - context.permissions
        if missing:
            await record(
                session,
                action=AuditAction.AUTHZ_DENIED,
                outcome=Outcome.BLOCKED,
                user_id=user.id,
                ip_address=client_ip(request),
                detail={
                    "organization_id": str(organization_id),
                    "role": context.role.value,
                    "missing": sorted(permission.value for permission in missing),
                    "path": request.url.path,
                },
                commit=True,
            )
            raise _forbidden(next(iter(sorted(missing))))

        return context

    return dependency


def require_member() -> OrgDependency:
    """Membership with no particular permission. For read-only org endpoints."""
    return require()


# ---------------------------------------------------------------------------
# Escalation guards
#
# These are separate from the permission check on purpose. MEMBER_ROLE_UPDATE
# says "may change roles at all"; it must not imply "may change *any* role to
# *any* value", or every admin is one request away from becoming an owner.
# ---------------------------------------------------------------------------


async def assert_can_assign(
    session: AsyncSession,
    context: OrgContext,
    target_role: Role,
) -> None:
    """Refuse to grant a role above the actor's own.

    Audited on rejection. A member with legitimate role-management authority
    reaching for a higher role is the clearest signal of an insider attack or a
    compromised account that this system produces — losing it to a bare 403
    would leave that entirely invisible.
    """
    if not context.role.can_grant(target_role):
        await record(
            session,
            action=AuditAction.AUTHZ_ESCALATION_BLOCKED,
            outcome=Outcome.BLOCKED,
            user_id=context.user.id,
            detail={
                "organization_id": str(context.organization_id),
                "actor_role": context.role.value,
                "attempted_role": target_role.value,
                "guard": "can_assign",
            },
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{_article(context.role).capitalize()} {context.role.value} "
                f"cannot grant the {target_role.value} role."
            ),
        )


async def assert_can_modify(
    session: AsyncSession,
    context: OrgContext,
    subject_role: Role,
) -> None:
    """Whether the actor may act on a member currently holding ``subject_role``.

    Equal ranks are allowed so admins can manage each other, but nobody may act
    on someone who outranks them — otherwise an admin could remove the owner.
    Audited on rejection, for the same reason as :func:`assert_can_assign`.
    """
    if subject_role.outranks(context.role):
        await record(
            session,
            action=AuditAction.AUTHZ_ESCALATION_BLOCKED,
            outcome=Outcome.BLOCKED,
            user_id=context.user.id,
            detail={
                "organization_id": str(context.organization_id),
                "actor_role": context.role.value,
                "subject_role": subject_role.value,
                "guard": "can_modify",
            },
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{_article(context.role).capitalize()} {context.role.value} cannot modify "
                f"{_article(subject_role)} {subject_role.value}."
            ),
        )


async def assert_not_last_owner(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    subject_user_id: uuid.UUID,
) -> None:
    """Refuse to remove or demote the final owner.

    An organisation with no owner cannot be administered by anyone, and no
    permission in this module can restore one — it is an unrecoverable state,
    so it is blocked rather than repaired.
    """
    owners = (
        (
            await session.execute(
                select(Membership.user_id).where(
                    Membership.organization_id == organization_id,
                    Membership.role == Role.OWNER.value,
                )
            )
        )
        .scalars()
        .all()
    )

    if subject_user_id in owners and len(owners) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This is the only owner. Promote another owner first.",
        )
