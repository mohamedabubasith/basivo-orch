"""The `/runs/{id}/events` endpoint.

The run detail page renders this in full — every model turn and tool call an
Agent node produced — so the two properties worth a regression test are order
(the UI trusts `seq` to render top-to-bottom) and tenant isolation (the same
property every other run-scoped endpoint has to hold).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.flows.events import EventWriter
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.router import run_events


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified; the gate runs after auth.
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def test_events_are_returned_in_sequence_order(session, organization, make_run):
    run = await make_run(Graph())
    writer = EventWriter(session, run.id, client=None)
    await writer.emit("node.step", {"node_id": "agent_1", "step": "agent.started"})
    await writer.emit("node.step", {"node_id": "agent_1", "step": "llm.response"})
    await writer.emit("node.step", {"node_id": "agent_1", "step": "agent.finished"})

    result = await run_events(
        run.id, after=0, limit=2000, context=make_context(organization), session=session
    )

    steps = [event["data"]["step"] for event in result["events"]]
    assert steps == ["agent.started", "llm.response", "agent.finished"]
    assert [event["seq"] for event in result["events"]] == [1, 2, 3]
    assert result["next_after"] == 3


async def test_after_excludes_already_seen_events(session, organization, make_run):
    run = await make_run(Graph())
    writer = EventWriter(session, run.id, client=None)
    await writer.emit("node.step", {"node_id": "agent_1", "step": "agent.started"})
    await writer.emit("node.step", {"node_id": "agent_1", "step": "agent.finished"})

    result = await run_events(
        run.id, after=1, limit=2000, context=make_context(organization), session=session
    )

    assert [event["data"]["step"] for event in result["events"]] == ["agent.finished"]


async def test_another_workspaces_run_is_a_404(session, organization, make_run):
    run = await make_run(Graph())

    other = Organization(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()
    await session.refresh(other)

    with pytest.raises(HTTPException) as raised:
        await run_events(run.id, after=0, limit=2000, context=make_context(other), session=session)
    assert raised.value.status_code == 404


async def test_unknown_run_is_a_404(session, organization):
    with pytest.raises(HTTPException) as raised:
        await run_events(
            uuid.uuid4(), after=0, limit=2000, context=make_context(organization), session=session
        )
    assert raised.value.status_code == 404
