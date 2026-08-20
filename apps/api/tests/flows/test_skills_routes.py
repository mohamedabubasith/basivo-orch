"""The skill library's HTTP surface.

Tested against the router functions directly, the same way the credential
routes are: `Depends(require(...))` is an ordinary Python dependency, so
calling the endpoint with a constructed `OrgContext` exercises the real path
without an HTTP client.

What matters here: a workspace can never reach another's library (skills carry
the prompt an agent follows, so a leak is a leak of process), the name stays
unique because a duplicate makes `load_skill` a coin toss, and importing a
hand-written file explains itself when it is wrong.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.skills.models import Skill
from basivo_orch.skills.router import (
    create_skill,
    delete_skill,
    export_skill,
    import_skill,
    list_skills,
    read_skill,
    skill_stats,
    update_skill,
)
from basivo_orch.skills.schemas import SkillImport, SkillWrite

ALL = frozenset(Permission)


def make_context(organization: Organization, *, permissions: frozenset[Permission] = ALL):
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified here.
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=permissions
    )


def a_skill(name: str = "refund-policy") -> SkillWrite:
    return SkillWrite(
        name=name,
        description="Use when a customer asks for money back, including chargebacks.",
        instructions="# Refunds\n\n1. Check the order date.",
    )


async def test_created_then_listed_and_read(session, organization):
    context = make_context(organization)
    created = await create_skill(a_skill(), context=context, session=session)

    listed = await list_skills(context=context, session=session)
    assert [entry.name for entry in listed] == ["refund-policy"]
    # The summary carries sizes, not bodies — a picker with thirty skills must
    # not transfer thirty procedures.
    assert listed[0].instruction_chars == len(created.instructions)
    assert not hasattr(listed[0], "instructions")

    full = await read_skill(created.id, context=context, session=session)
    assert full.instructions.startswith("# Refunds")


async def test_a_second_skill_cannot_take_the_same_name(session, organization):
    context = make_context(organization)
    await create_skill(a_skill(), context=context, session=session)

    with pytest.raises(HTTPException) as raised:
        await create_skill(a_skill(), context=context, session=session)
    assert raised.value.status_code == 409
    assert "how an agent asks for it" in raised.value.detail


async def test_one_workspace_cannot_read_anothers_library(session, organization):
    """A skill is the process an agent follows. Cross-tenant reads are a leak
    of how another company works, not merely of a row."""
    mine = make_context(organization)
    created = await create_skill(a_skill(), context=mine, session=session)

    other = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()
    theirs = make_context(other)

    assert await list_skills(context=theirs, session=session) == []
    for call in (read_skill, export_skill):
        with pytest.raises(HTTPException) as raised:
            await call(created.id, context=theirs, session=session)
        assert raised.value.status_code == 404
    with pytest.raises(HTTPException):
        await delete_skill(created.id, context=theirs, session=session)

    # Still there afterwards.
    assert len(await list_skills(context=mine, session=session)) == 1


async def test_import_accepts_a_hand_written_file(session, organization):
    context = make_context(organization)
    skill = await import_skill(
        SkillImport(
            content="""---
name: incident-review
description: Use after a production incident, when writing the postmortem.
---

# Incident review

Start with the timeline.
"""
        ),
        context=context,
        session=session,
    )
    assert skill.name == "incident-review"
    assert skill.instructions.startswith("# Incident review")


async def test_import_explains_a_bad_file_instead_of_500ing(session, organization):
    context = make_context(organization)
    with pytest.raises(HTTPException) as raised:
        await import_skill(
            SkillImport(content="# No frontmatter at all\n\nJust a document."),
            context=context,
            session=session,
        )
    assert raised.value.status_code == 422
    assert "frontmatter" in raised.value.detail


async def test_export_is_the_file_it_was_imported_from(session, organization):
    context = make_context(organization)
    created = await create_skill(a_skill(), context=context, session=session)

    response = await export_skill(created.id, context=context, session=session)
    body = response.body.decode()
    assert body.startswith("---\nname: refund-policy\n")
    assert "1. Check the order date." in body
    assert "refund-policy.SKILL.md" in response.headers["content-disposition"]


async def test_update_replaces_the_body(session, organization):
    context = make_context(organization)
    created = await create_skill(a_skill(), context=context, session=session)

    updated = await update_skill(
        created.id,
        SkillWrite(
            name="refund-policy",
            description="Use when a customer asks for money back. Now with escalation.",
            instructions="# Refunds\n\nAsk the duty lead.",
            resources=[{"name": "policy.md", "content": "The long version."}],
        ),
        context=context,
        session=session,
    )
    assert updated.instructions == "# Refunds\n\nAsk the duty lead."
    assert updated.resources[0]["name"] == "policy.md"


async def test_stats_count_the_library_and_its_use(session, organization):
    context = make_context(organization)
    first = await create_skill(a_skill(), context=context, session=session)
    await create_skill(a_skill("escalation"), context=context, session=session)
    first.load_count = 3
    await session.commit()

    assert await skill_stats(context=context, session=session) == {"skills": 2, "loads": 3}


async def test_a_viewer_may_read_but_not_write(session, organization):
    """Reading the library is how a reviewer answers "why did the agent say
    that" without the authority to change the answer."""
    from basivo_orch.auth.authz import ROLE_PERMISSIONS

    viewer = ROLE_PERMISSIONS[Role.VIEWER]
    assert Permission.SKILL_READ in viewer
    assert Permission.SKILL_WRITE not in viewer
    assert Permission.SKILL_DELETE not in ROLE_PERMISSIONS[Role.MEMBER]


async def test_deleting_a_skill_leaves_the_flow_running(session, organization):
    """Flows reference skills by id; deleting one must not need a flow rewrite.
    The engine skips what it cannot find — proven in the integration suite."""
    context = make_context(organization)
    created = await create_skill(a_skill(), context=context, session=session)
    await delete_skill(created.id, context=context, session=session)

    assert await list_skills(context=context, session=session) == []
    assert (await session.get(Skill, created.id)) is None
