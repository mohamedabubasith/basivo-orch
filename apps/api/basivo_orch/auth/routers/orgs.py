"""Organisation and membership management.

Every route below is scoped by the ``organization_id`` path parameter, and the
authority to act on it comes from ``Depends(require(...))`` — which resolves the
caller's membership for *that* organisation. A route can therefore never operate
on one tenant while having been authorised against another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import (
    ROLE_PERMISSIONS,
    OrgContext,
    Permission,
    Role,
    assert_can_assign,
    assert_can_modify,
    assert_not_last_owner,
    require,
)
from basivo_orch.auth.db import get_async_session
from basivo_orch.auth.engine import current_active_user
from basivo_orch.auth.models import Membership, Organization, User
from basivo_orch.auth.schemas import (
    MemberInvite,
    MemberRead,
    MemberRoleUpdate,
    MessageResponse,
    OrganizationCreate,
    OrganizationRead,
    OrganizationSummary,
    OrganizationUpdate,
)
from basivo_orch.auth.security.audit import AuditAction, Outcome, record
from basivo_orch.auth.security.ratelimit import client_ip, limiter

router = APIRouter(prefix="/orgs", tags=["organisations"])


def _parse_role(raw: str) -> Role:
    try:
        return Role(raw.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown role. Valid roles: {', '.join(role.value for role in Role)}.",
        ) from None


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


@router.get("", response_model=list[OrganizationSummary])
async def list_my_organizations(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> list[OrganizationSummary]:
    """Every organisation the caller belongs to.

    Not permission-gated, because it is inherently self-scoped: the query is
    filtered by the caller's own memberships and can only ever return their own.
    """
    rows = (
        await session.execute(
            select(Organization, Membership)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == user.id, Organization.is_active.is_(True))
            .order_by(Organization.name)
        )
    ).all()

    summaries: list[OrganizationSummary] = []
    for organization, membership in rows:
        try:
            role = Role(membership.role)
        except ValueError:
            continue  # Unknown role: fail closed, omit rather than guess.
        summaries.append(
            OrganizationSummary(
                id=organization.id,
                name=organization.name,
                slug=organization.slug,
                is_active=organization.is_active,
                created_at=organization.created_at,
                role=role.value,
                permissions=sorted(p.value for p in ROLE_PERMISSIONS[role]),
            )
        )
    return summaries


@router.post("", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
async def create_organization(
    request: Request,
    response: Response,
    payload: OrganizationCreate,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> OrganizationRead:
    """Create an organisation. The creator becomes its owner."""
    organization = Organization(name=payload.name, slug=payload.slug, is_active=True)
    session.add(organization)

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That slug is already taken.",
        ) from None

    # Created in the same transaction as the organisation. If this failed
    # separately the org would exist with no owner — a state no permission in
    # the system can repair.
    session.add(
        Membership(
            user_id=user.id,
            organization_id=organization.id,
            role=Role.OWNER.value,
        )
    )

    await record(
        session,
        action=AuditAction.ORG_CREATED,
        outcome=Outcome.SUCCESS,
        user_id=user.id,
        ip_address=client_ip(request),
        detail={"organization_id": str(organization.id), "slug": organization.slug},
    )
    await session.commit()
    return OrganizationRead.model_validate(organization)


@router.get("/{organization_id}", response_model=OrganizationSummary)
async def get_organization(
    context: OrgContext = Depends(require(Permission.ORG_READ)),
) -> OrganizationSummary:
    return OrganizationSummary(
        id=context.organization.id,
        name=context.organization.name,
        slug=context.organization.slug,
        is_active=context.organization.is_active,
        created_at=context.organization.created_at,
        role=context.role.value,
        permissions=sorted(p.value for p in context.permissions),
    )


@router.patch("/{organization_id}", response_model=OrganizationRead)
async def update_organization(
    request: Request,
    payload: OrganizationUpdate,
    context: OrgContext = Depends(require(Permission.ORG_UPDATE)),
    session: AsyncSession = Depends(get_async_session),
) -> OrganizationRead:
    """Rename or suspend an organisation.

    The slug is intentionally immutable: it may be embedded in customer
    bookmarks, webhook targets and subdomains, and silently freeing an old slug
    for someone else to claim is an impersonation vector.
    """
    if payload.name is not None:
        context.organization.name = payload.name

    if payload.is_active is not None:
        # Suspension hides the org from everyone including admins, so it takes
        # owner authority rather than the general update permission.
        context.require(Permission.ORG_DELETE)
        context.organization.is_active = payload.is_active

    await record(
        session,
        action=AuditAction.ORG_UPDATED,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={
            "organization_id": str(context.organization_id),
            "fields": [k for k, v in payload.model_dump().items() if v is not None],
        },
    )
    await session.commit()
    return OrganizationRead.model_validate(context.organization)


@router.delete("/{organization_id}", response_model=MessageResponse)
async def delete_organization(
    request: Request,
    context: OrgContext = Depends(require(Permission.ORG_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Deactivate an organisation.

    A soft delete. Hard-deleting would cascade through every member's data with
    no way back; deactivating makes the org invisible (``load_context`` treats
    inactive as non-existent) while the data remains recoverable.
    """
    context.organization.is_active = False
    await record(
        session,
        action=AuditAction.ORG_DELETED,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={"organization_id": str(context.organization_id)},
    )
    await session.commit()
    return MessageResponse(detail="Organisation deactivated.")


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{organization_id}/members", response_model=list[MemberRead])
async def list_members(
    context: OrgContext = Depends(require(Permission.MEMBER_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[MemberRead]:
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            # The tenant filter. Every org-scoped query needs this line; without
            # it the endpoint returns every member of every customer.
            .where(Membership.organization_id == context.organization_id)
            .order_by(User.email)
        )
    ).all()

    return [
        MemberRead(
            user_id=user.id,
            email=user.email,
            role=membership.role,
            created_at=membership.created_at,
            invited_by_id=membership.invited_by_id,
        )
        for membership, user in rows
    ]


@router.post(
    "/{organization_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("30/hour")
async def invite_member(
    request: Request,
    response: Response,
    payload: MemberInvite,
    context: OrgContext = Depends(require(Permission.MEMBER_INVITE)),
    session: AsyncSession = Depends(get_async_session),
) -> MemberRead:
    """Add an existing user to the organisation.

    Requires the invitee to already have an account. Creating one here would let
    any org admin mint accounts on arbitrary addresses, which is both a spam
    vector and a way to squat an address before its real owner registers.
    """
    target_role = _parse_role(payload.role)
    # Permission to invite is not permission to invite *at any level*.
    await assert_can_assign(session, context, target_role)

    invitee = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    if invitee is None or not invitee.is_active:
        # Same response either way: a differing one turns this endpoint into a
        # membership oracle for the whole user table.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active account exists for that address. Ask them to sign up first.",
        )

    existing = (
        await session.execute(
            select(Membership).where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == invitee.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That user is already a member.",
        )

    membership = Membership(
        user_id=invitee.id,
        organization_id=context.organization_id,
        role=target_role.value,
        invited_by_id=context.user.id,
    )
    session.add(membership)
    await session.flush()

    await record(
        session,
        action=AuditAction.MEMBER_INVITED,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={
            "organization_id": str(context.organization_id),
            "member_id": str(invitee.id),
            "role": target_role.value,
        },
    )
    await session.commit()

    return MemberRead(
        user_id=invitee.id,
        email=invitee.email,
        role=membership.role,
        created_at=membership.created_at,
        invited_by_id=context.user.id,
    )


@router.patch("/{organization_id}/members/{member_id}", response_model=MemberRead)
async def update_member_role(
    request: Request,
    member_id: uuid.UUID,
    payload: MemberRoleUpdate,
    context: OrgContext = Depends(require(Permission.MEMBER_ROLE_UPDATE)),
    session: AsyncSession = Depends(get_async_session),
) -> MemberRead:
    """Change a member's role.

    Four separate guards apply, and all of them are needed:

    * you may not grant a role above your own (``assert_can_assign``)
    * you may not act on someone who outranks you (``assert_can_modify``)
    * you may not demote the last owner (``assert_not_last_owner``)
    * you may not change your own role at all
    """
    target_role = _parse_role(payload.role)

    if member_id == context.user.id:
        # Self-modification is the shortest path to privilege escalation, and
        # there is no legitimate use for it: promotion needs someone senior,
        # demotion is `leave`.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot change your own role.",
        )

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == member_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    current_role = _parse_role(membership.role)
    await assert_can_modify(session, context, current_role)
    await assert_can_assign(session, context, target_role)

    if current_role is Role.OWNER and target_role is not Role.OWNER:
        await assert_not_last_owner(
            session,
            organization_id=context.organization_id,
            subject_user_id=member_id,
        )

    membership.role = target_role.value
    membership.updated_at = datetime.now(UTC)

    await record(
        session,
        action=AuditAction.MEMBER_ROLE_CHANGED,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={
            "organization_id": str(context.organization_id),
            "member_id": str(member_id),
            "from": current_role.value,
            "to": target_role.value,
        },
    )
    await session.commit()

    user = await session.get(User, member_id)
    return MemberRead(
        user_id=member_id,
        email=user.email if user else "",
        role=membership.role,
        created_at=membership.created_at,
        invited_by_id=membership.invited_by_id,
    )


@router.delete("/{organization_id}/members/{member_id}", response_model=MessageResponse)
async def remove_member(
    request: Request,
    member_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.MEMBER_REMOVE)),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    if member_id == context.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use the leave endpoint to remove yourself.",
        )

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == member_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")

    await assert_can_modify(session, context, _parse_role(membership.role))
    await assert_not_last_owner(
        session,
        organization_id=context.organization_id,
        subject_user_id=member_id,
    )

    await session.delete(membership)
    await record(
        session,
        action=AuditAction.MEMBER_REMOVED,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={"organization_id": str(context.organization_id), "member_id": str(member_id)},
    )
    await session.commit()
    return MessageResponse(detail="Member removed.")


@router.post("/{organization_id}/leave", response_model=MessageResponse)
async def leave_organization(
    request: Request,
    context: OrgContext = Depends(require()),
    session: AsyncSession = Depends(get_async_session),
) -> MessageResponse:
    """Remove yourself from an organisation.

    Needs membership but no permission — a viewer must be able to leave. The
    last owner still cannot, or the org would be left unadministrable.
    """
    await assert_not_last_owner(
        session,
        organization_id=context.organization_id,
        subject_user_id=context.user.id,
    )

    membership = (
        await session.execute(
            select(Membership).where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == context.user.id,
            )
        )
    ).scalar_one_or_none()
    if membership is not None:
        await session.delete(membership)

    await record(
        session,
        action=AuditAction.MEMBER_LEFT,
        outcome=Outcome.SUCCESS,
        user_id=context.user.id,
        ip_address=client_ip(request),
        detail={"organization_id": str(context.organization_id)},
    )
    await session.commit()
    return MessageResponse(detail="You have left the organisation.")
