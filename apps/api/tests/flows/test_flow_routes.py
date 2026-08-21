"""The flow management routes, at the seam where the ORM meets serialisation.

Renaming a flow returned a 500, and the reason is worth a test rather than a
comment: `Flow.updated_at` is `onupdate=func.now()`, so after an UPDATE the new
value exists only in the database. The ORM marks the attribute expired —
`expire_on_commit=False` does not help, because it was never loaded with the new
value — and pydantic then reads it during serialisation, from sync code, which
is a lazy SELECT it cannot perform.

The graph-saving path never hit this because it assigns `updated_at` in Python.
Which is exactly what made the bug easy to write: one path proves the other
looks fine.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.flows import service
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.router import update_flow
from basivo_orch.flows.schemas import FlowUpdate

ALL = frozenset(Permission)

TRIVIAL_GRAPH = {
    "nodes": [
        {"id": "t", "type": "trigger.manual", "config": {}},
        {"id": "set", "type": "data.set", "config": {"assignments": [{"name": "x", "value": "1"}]}},
    ],
    "edges": [{"source": "t", "target": "set"}],
}


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified here.
    return OrgContext(user=user, organization=organization, role=Role.OWNER, permissions=ALL)


async def a_flow(session, organization: Organization, name: str = "Untitled flow"):
    flow, _ = await service.create_flow(
        session,
        organization_id=organization.id,
        user_id=None,
        name=name,
        slug=None,
        description=None,
        graph=Graph.model_validate(TRIVIAL_GRAPH),
    )
    return flow


async def test_renaming_a_flow_without_touching_the_graph(session, organization):
    """The 500. A name is not a graph edit, so it arrives on its own."""
    flow = await a_flow(session, organization)
    context = make_context(organization)

    result = await update_flow(
        flow.id, FlowUpdate(name="Renamed in place"), context=context, session=session
    )

    assert result.name == "Renamed in place"
    # The field that could not be read is in the response, which is the whole
    # point: serialising it is what used to fail.
    assert result.updated_at is not None
    assert result.version == 1, "renaming must not create a new version"


async def test_renaming_does_not_disturb_the_graph(session, organization):
    flow = await a_flow(session, organization)
    context = make_context(organization)

    result = await update_flow(
        flow.id, FlowUpdate(name="Still working"), context=context, session=session
    )

    assert [node.id for node in result.graph.nodes] == ["t", "set"]


async def test_a_description_alone_also_serialises(session, organization):
    """The same path, reached by the other field on it."""
    flow = await a_flow(session, organization)
    context = make_context(organization)

    result = await update_flow(
        flow.id,
        FlowUpdate(description="What it does and who depends on it"),
        context=context,
        session=session,
    )
    assert result.description == "What it does and who depends on it"
    assert result.updated_at is not None


async def test_saving_a_graph_still_makes_a_version(session, organization):
    """The path that always worked, kept honest."""
    flow = await a_flow(session, organization)
    context = make_context(organization)

    graph = dict(TRIVIAL_GRAPH)
    graph["nodes"] = [*TRIVIAL_GRAPH["nodes"], {"id": "set2", "type": "data.set", "config": {}}]
    graph["edges"] = [*TRIVIAL_GRAPH["edges"], {"source": "set", "target": "set2"}]

    result = await update_flow(
        flow.id, FlowUpdate(graph=Graph.model_validate(graph)), context=context, session=session
    )
    assert result.version == 2
    assert len(result.graph.nodes) == 3


async def test_another_workspace_cannot_rename_this_flow(session, organization):
    flow = await a_flow(session, organization)
    intruder = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(intruder)
    await session.commit()

    with pytest.raises(HTTPException) as raised:
        await update_flow(
            flow.id, FlowUpdate(name="Mine now"), context=make_context(intruder), session=session
        )
    assert raised.value.status_code == 404


async def test_a_second_untitled_flow_gets_its_own_slug(session, organization):
    """One-click creation names every new flow the same thing.

    The slug is derived from the name, so the second one collided and the
    button simply failed with a 409 — which is what happens when an internal
    detail is allowed to become the user's problem.
    """
    first = await a_flow(session, organization, name="Untitled flow")
    second = await a_flow(session, organization, name="Untitled flow")
    third = await a_flow(session, organization, name="Untitled flow")

    assert first.slug == "untitled-flow"
    assert second.slug == "untitled-flow-2"
    assert third.slug == "untitled-flow-3"
    assert first.name == second.name == "Untitled flow", "only the slug is adjusted"


async def test_a_slug_the_caller_asked_for_still_conflicts(session, organization):
    """The other half of the rule: an address someone chose is theirs, and
    silently handing them a different one would break whatever they pointed at
    it."""
    await service.create_flow(
        session,
        organization_id=organization.id,
        user_id=None,
        name="Nightly poster",
        slug="nightly",
        description=None,
        graph=Graph.model_validate(TRIVIAL_GRAPH),
    )

    with pytest.raises(ValueError, match="already exists"):
        await service.create_flow(
            session,
            organization_id=organization.id,
            user_id=None,
            name="Something else",
            slug="nightly",
            description=None,
            graph=Graph.model_validate(TRIVIAL_GRAPH),
        )
