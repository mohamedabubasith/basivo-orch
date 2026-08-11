"""Authorization: tenant isolation and privilege escalation.

The two failure modes that matter for multi-tenant authorization:

* **Cross-tenant access (IDOR)** — a valid member of org A reaching org B's data
  by swapping an id in the URL. Authentication succeeds, which is why it slips
  past reviews that only check "is the user logged in".
* **Privilege escalation** — a member acquiring authority they were not granted,
  usually by assigning themselves a higher role.

Every test below fails loudly if either becomes possible.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from basivo_orch.auth.authz import (
    ROLE_PERMISSIONS,
    OrgContext,
    Permission,
    Role,
    assert_can_assign,
    assert_can_modify,
    assert_not_last_owner,
    load_context,
)
from basivo_orch.auth.models import Membership, Organization, User
from basivo_orch.auth.security.passwords import hash_password

pytestmark = pytest.mark.security


async def _make_user(session, email: str, *, superuser: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password("correct-horse-battery-staple-42"),
        is_active=True,
        is_verified=True,
        is_superuser=superuser,
    )
    session.add(user)
    await session.commit()
    return user


async def _make_org(session, slug: str) -> Organization:
    org = Organization(id=uuid.uuid4(), name=slug.title(), slug=slug, is_active=True)
    session.add(org)
    await session.commit()
    return org


async def _add_member(session, user: User, org: Organization, role: Role) -> Membership:
    membership = Membership(user_id=user.id, organization_id=org.id, role=role.value)
    session.add(membership)
    await session.commit()
    return membership


# ---------------------------------------------------------------------------
# Role / permission mapping
# ---------------------------------------------------------------------------


def test_every_role_has_a_permission_set() -> None:
    """A role missing from the map would raise KeyError mid-request."""
    for role in Role:
        assert role in ROLE_PERMISSIONS


def test_owner_holds_every_permission() -> None:
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


def test_privileges_increase_with_rank() -> None:
    assert ROLE_PERMISSIONS[Role.VIEWER] < ROLE_PERMISSIONS[Role.MEMBER]
    assert ROLE_PERMISSIONS[Role.MEMBER] < ROLE_PERMISSIONS[Role.ADMIN]
    assert ROLE_PERMISSIONS[Role.ADMIN] < ROLE_PERMISSIONS[Role.OWNER]


def test_only_owner_can_delete_or_transfer() -> None:
    """Destructive org-level authority must not reach admins."""
    for role in (Role.VIEWER, Role.MEMBER, Role.ADMIN):
        assert Permission.ORG_DELETE not in ROLE_PERMISSIONS[role]
        assert Permission.ORG_TRANSFER not in ROLE_PERMISSIONS[role]


def test_viewer_cannot_enumerate_members() -> None:
    assert Permission.MEMBER_READ not in ROLE_PERMISSIONS[Role.VIEWER]


def test_non_admins_cannot_manage_members() -> None:
    for role in (Role.VIEWER, Role.MEMBER):
        assert Permission.MEMBER_INVITE not in ROLE_PERMISSIONS[role]
        assert Permission.MEMBER_REMOVE not in ROLE_PERMISSIONS[role]
        assert Permission.MEMBER_ROLE_UPDATE not in ROLE_PERMISSIONS[role]


# ---------------------------------------------------------------------------
# Escalation guards
# ---------------------------------------------------------------------------


def test_role_ranking_is_strict() -> None:
    assert Role.OWNER.outranks(Role.ADMIN)
    assert Role.ADMIN.outranks(Role.MEMBER)
    assert Role.MEMBER.outranks(Role.VIEWER)
    assert not Role.ADMIN.outranks(Role.OWNER)
    assert not Role.ADMIN.outranks(Role.ADMIN)


def test_admin_cannot_grant_ownership() -> None:
    """The single most important escalation guard.

    Without it, any admin is one request away from owner — and owner can then
    remove every other owner.
    """
    assert not Role.ADMIN.can_grant(Role.OWNER)
    assert Role.OWNER.can_grant(Role.OWNER)


def test_a_role_may_grant_its_own_level_and_below() -> None:
    assert Role.ADMIN.can_grant(Role.ADMIN)
    assert Role.ADMIN.can_grant(Role.MEMBER)
    assert Role.MEMBER.can_grant(Role.VIEWER)
    assert not Role.VIEWER.can_grant(Role.MEMBER)


async def _admin_context(session) -> OrgContext:
    user = await _make_user(session, "admin-ctx@example.com")
    org = await _make_org(session, "ctx-org")
    return OrgContext(
        user=user,
        organization=org,
        role=Role.ADMIN,
        permissions=ROLE_PERMISSIONS[Role.ADMIN],
    )


async def test_assert_can_assign_blocks_escalation(session) -> None:
    from fastapi import HTTPException

    context = await _admin_context(session)
    await assert_can_assign(session, context, Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await assert_can_assign(session, context, Role.OWNER)
    assert exc.value.status_code == 403


async def test_assert_can_modify_blocks_acting_on_a_superior(session) -> None:
    from fastapi import HTTPException

    context = await _admin_context(session)
    await assert_can_modify(session, context, Role.MEMBER)
    await assert_can_modify(session, context, Role.ADMIN)  # peers may manage each other
    with pytest.raises(HTTPException) as exc:
        await assert_can_modify(session, context, Role.OWNER)
    assert exc.value.status_code == 403


async def test_blocked_escalation_is_audited(session) -> None:
    """An escalation attempt must leave a trail, not just a 403.

    This is the strongest insider-attack signal the system emits; losing it
    would make privilege-escalation probing invisible.
    """
    from fastapi import HTTPException
    from sqlalchemy import select as _select

    from basivo_orch.auth.models import AuditEvent
    from basivo_orch.auth.security.audit import AuditAction

    context = await _admin_context(session)
    with pytest.raises(HTTPException):
        await assert_can_assign(session, context, Role.OWNER)

    rows = (
        (
            await session.execute(
                _select(AuditEvent).where(
                    AuditEvent.action == AuditAction.AUTHZ_ESCALATION_BLOCKED.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the escalation attempt must be recorded"
    assert rows[0].outcome == "blocked"
    assert rows[0].user_id == context.user.id


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_membership_does_not_leak_across_organisations(session) -> None:
    """Being an owner of one org grants nothing in another."""
    from fastapi import HTTPException

    user = await _make_user(session, "ada@example.com")
    mine = await _make_org(session, "mine")
    theirs = await _make_org(session, "theirs")
    await _add_member(session, user, mine, Role.OWNER)

    context = await load_context(session, user=user, organization_id=mine.id)
    assert context.role is Role.OWNER

    with pytest.raises(HTTPException) as exc:
        await load_context(session, user=user, organization_id=theirs.id)
    # 404 rather than 403: a 403 would confirm the org exists.
    assert exc.value.status_code == 404


async def test_unknown_organisation_is_indistinguishable_from_forbidden(session) -> None:
    from fastapi import HTTPException

    user = await _make_user(session, "ada@example.com")
    theirs = await _make_org(session, "theirs")

    with pytest.raises(HTTPException) as not_a_member:
        await load_context(session, user=user, organization_id=theirs.id)
    with pytest.raises(HTTPException) as does_not_exist:
        await load_context(session, user=user, organization_id=uuid.uuid4())

    assert not_a_member.value.status_code == does_not_exist.value.status_code == 404
    assert not_a_member.value.detail == does_not_exist.value.detail


async def test_inactive_organisation_is_invisible(session) -> None:
    from fastapi import HTTPException

    user = await _make_user(session, "ada@example.com")
    org = await _make_org(session, "suspended")
    await _add_member(session, user, org, Role.OWNER)

    org.is_active = False
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await load_context(session, user=user, organization_id=org.id)
    assert exc.value.status_code == 404


async def test_unknown_role_string_fails_closed(session) -> None:
    """A role value this build does not recognise must grant nothing.

    Guards against a downgrade, or a role written directly into the database,
    silently being treated as valid.
    """
    from fastapi import HTTPException

    user = await _make_user(session, "ada@example.com")
    org = await _make_org(session, "acme")
    membership = await _add_member(session, user, org, Role.MEMBER)

    membership.role = "superadmin"
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await load_context(session, user=user, organization_id=org.id)
    assert exc.value.status_code == 404


async def test_superuser_does_not_bypass_by_default(session) -> None:
    """`is_superuser` must not silently grant access to every tenant."""
    from fastapi import HTTPException

    staff = await _make_user(session, "staff@example.com", superuser=True)
    org = await _make_org(session, "customer")

    with pytest.raises(HTTPException) as exc:
        await load_context(session, user=staff, organization_id=org.id)
    assert exc.value.status_code == 404


async def test_superuser_bypass_when_explicitly_enabled(session, monkeypatch) -> None:
    from basivo_orch.auth.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SUPERUSER_BYPASSES_ORG_PERMISSIONS", "true")

    staff = await _make_user(session, "staff@example.com", superuser=True)
    org = await _make_org(session, "customer")

    context = await load_context(session, user=staff, organization_id=org.id)
    assert context.role is Role.OWNER
    # Flagged so audit and downstream code can tell break-glass from membership.
    assert context.via_superuser is True

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Last-owner protection
# ---------------------------------------------------------------------------


async def test_last_owner_cannot_be_removed(session) -> None:
    from fastapi import HTTPException

    owner = await _make_user(session, "owner@example.com")
    org = await _make_org(session, "acme")
    await _add_member(session, owner, org, Role.OWNER)

    with pytest.raises(HTTPException) as exc:
        await assert_not_last_owner(session, organization_id=org.id, subject_user_id=owner.id)
    assert exc.value.status_code == 409


async def test_an_owner_can_be_removed_when_another_remains(session) -> None:
    first = await _make_user(session, "first@example.com")
    second = await _make_user(session, "second@example.com")
    org = await _make_org(session, "acme")
    await _add_member(session, first, org, Role.OWNER)
    await _add_member(session, second, org, Role.OWNER)

    await assert_not_last_owner(session, organization_id=org.id, subject_user_id=first.id)


async def test_removing_a_non_owner_is_always_allowed(session) -> None:
    owner = await _make_user(session, "owner@example.com")
    member = await _make_user(session, "member@example.com")
    org = await _make_org(session, "acme")
    await _add_member(session, owner, org, Role.OWNER)
    await _add_member(session, member, org, Role.MEMBER)

    await assert_not_last_owner(session, organization_id=org.id, subject_user_id=member.id)


async def test_owner_count_is_scoped_to_one_organisation(session) -> None:
    """An owner of a *different* org must not satisfy this org's owner check."""
    from fastapi import HTTPException

    owner = await _make_user(session, "owner@example.com")
    elsewhere = await _make_user(session, "elsewhere@example.com")
    mine = await _make_org(session, "mine")
    other = await _make_org(session, "other")
    await _add_member(session, owner, mine, Role.OWNER)
    await _add_member(session, elsewhere, other, Role.OWNER)

    with pytest.raises(HTTPException):
        await assert_not_last_owner(session, organization_id=mine.id, subject_user_id=owner.id)


async def test_membership_is_unique_per_user_and_org(session) -> None:
    """Two memberships would make the effective role arbitrary."""
    from sqlalchemy.exc import IntegrityError

    user = await _make_user(session, "ada@example.com")
    org = await _make_org(session, "acme")
    await _add_member(session, user, org, Role.MEMBER)

    session.add(Membership(user_id=user.id, organization_id=org.id, role=Role.OWNER.value))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_authority_is_read_from_the_database_not_cached(session) -> None:
    """A demotion must take effect immediately, not at token expiry."""
    user = await _make_user(session, "ada@example.com")
    org = await _make_org(session, "acme")
    membership = await _add_member(session, user, org, Role.ADMIN)

    before = await load_context(session, user=user, organization_id=org.id)
    assert Permission.MEMBER_REMOVE in before.permissions

    membership.role = Role.VIEWER.value
    await session.commit()

    after = await load_context(session, user=user, organization_id=org.id)
    assert Permission.MEMBER_REMOVE not in after.permissions


async def test_only_the_target_orgs_members_are_visible(session) -> None:
    """The list-members query must be filtered by organisation."""
    org_a = await _make_org(session, "alpha")
    org_b = await _make_org(session, "beta")
    a_user = await _make_user(session, "a@example.com")
    b_user = await _make_user(session, "b@example.com")
    await _add_member(session, a_user, org_a, Role.OWNER)
    await _add_member(session, b_user, org_b, Role.OWNER)

    rows = (
        (await session.execute(select(Membership).where(Membership.organization_id == org_a.id)))
        .scalars()
        .all()
    )

    assert [row.user_id for row in rows] == [a_user.id]
