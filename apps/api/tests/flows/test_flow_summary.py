"""What the flow list needs to know, gathered in a fixed number of queries.

The list previously showed a name, a slug and "updated 5 days ago" — none of
which answers the questions the page is opened with. These assert the three
that do, and that computing them does not become one query per row.
"""

from __future__ import annotations

import uuid

from basivo_orch.flows import service
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Flow, FlowVersion, RunStatus, TriggerKind

WEBHOOK_FLOW = Graph.model_validate(
    {
        "nodes": [
            {"id": "hook", "type": "trigger.webhook", "config": {}},
            {"id": "set", "type": "data.set", "config": {"assignments": []}},
            {"id": "note", "type": "data.set", "config": {"assignments": []}},
        ],
        "edges": [{"source": "hook", "target": "set"}, {"source": "set", "target": "note"}],
    }
)


async def make_flow(session, organization, graph: Graph, *, publish: bool = False) -> Flow:
    flow = Flow(
        organization_id=organization.id, name="Listed", slug=f"listed-{uuid.uuid4().hex[:8]}"
    )
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
    session.add(version)
    await session.flush()
    if publish:
        flow.published_version_id = version.id
    await session.commit()
    return flow


async def test_a_row_knows_its_size_and_what_starts_it(session, organization):
    flow = await make_flow(session, organization, WEBHOOK_FLOW, publish=True)

    summary = (await service.summarise_flows(session, [flow]))[flow.id]

    assert summary["node_count"] == 3
    assert summary["trigger_type"] == "trigger.webhook"
    # Never run, and said so rather than left as a zero that reads as success.
    assert summary["last_run_status"] is None
    assert summary["last_run_at"] is None


async def test_a_row_reports_the_most_recent_run_not_the_first(session, organization):
    flow = await make_flow(session, organization, WEBHOOK_FLOW, publish=True)
    version = await service.latest_version(session, flow.id)

    for status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
        run, _ = await service.create_run(
            session, flow=flow, version=version, trigger=TriggerKind.MANUAL, payload={}
        )
        run.status = status
        await session.commit()

    summary = (await service.summarise_flows(session, [flow]))[flow.id]
    assert summary["last_run_status"] == RunStatus.FAILED.value, (
        "the list showed an older run than the latest"
    )


async def test_an_empty_flow_is_reported_as_empty_rather_than_broken(session, organization):
    empty = Graph.model_validate({"nodes": [], "edges": []})
    flow = await make_flow(session, organization, empty)

    summary = (await service.summarise_flows(session, [flow]))[flow.id]
    assert summary["node_count"] == 0
    assert summary["trigger_type"] is None


async def test_summarising_many_flows_does_not_scale_the_query_count(session, organization):
    """Three queries for the page, whatever its length.

    A list that asks per row is how twenty flows becomes a two-second page.
    """
    flows = [await make_flow(session, organization, WEBHOOK_FLOW) for _ in range(6)]

    executed: list[str] = []
    original = session.execute

    async def counting(statement, *args, **kwargs):
        executed.append(str(statement).split("\n")[0][:40])
        return await original(statement, *args, **kwargs)

    session.execute = counting  # type: ignore[method-assign]
    try:
        summaries = await service.summarise_flows(session, flows)
    finally:
        session.execute = original  # type: ignore[method-assign]

    assert len(summaries) == 6
    assert len(executed) <= 4, f"one query per row crept in: {executed}"


async def test_no_flows_means_no_queries(session):
    assert await service.summarise_flows(session, []) == {}
