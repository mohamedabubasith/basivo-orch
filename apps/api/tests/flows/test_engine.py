"""Engine behaviour, and the run log it is required to leave behind."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import replay
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import NodeExecution, NodeStatus, RunStatus

TRIGGER = {"id": "t", "type": "trigger.manual", "config": {}}


def graph_of(nodes: list[dict], edges: list[dict]) -> Graph:
    return Graph.model_validate({"nodes": [TRIGGER, *nodes], "edges": edges})


def setter(node_id: str, name: str, value: object) -> dict:
    return {
        "id": node_id,
        "type": "data.set",
        "name": node_id,
        "config": {"assignments": [{"name": name, "value": value}]},
    }


def condition(node_id: str, left: object, op: str, right: object) -> dict:
    return {
        "id": node_id,
        "type": "logic.condition",
        "config": {"comparisons": [{"left": left, "operator": op, "right": right}]},
    }


async def run_graph(session: AsyncSession, make_run, graph: Graph, payload: dict | None = None):
    run = await make_run(graph, payload)
    return await Engine(session, run=run, graph=graph, redis_client=None).execute()


async def nodes_for(session: AsyncSession, run_id) -> dict[str, NodeExecution]:
    result = await session.execute(select(NodeExecution).where(NodeExecution.run_id == run_id))
    return {row.node_id: row for row in result.scalars()}


# ---------------------------------------------------------------------------


async def test_a_linear_flow_succeeds_and_carries_data(session, make_run) -> None:
    graph = graph_of(
        [setter("a", "greeting", "hello"), setter("b", "shout", "{{ vars.greeting }} there")],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "b"}],
    )
    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.SUCCEEDED
    assert run.output == {"result": {"greeting": "hello", "shout": "hello there"}}
    assert run.duration_ms is not None


async def test_trigger_payload_reaches_the_first_node(session, make_run) -> None:
    graph = graph_of(
        [setter("a", "who", "{{ trigger.payload.name }}")], [{"source": "t", "target": "a"}]
    )
    run = await run_graph(session, make_run, graph, {"name": "Ada"})
    # The Set node merges into what it received, and what it received is the
    # trigger's output — so the payload is still there alongside the new field.
    assert run.output == {"result": {"name": "Ada", "who": "Ada"}}


@pytest.mark.security
async def test_every_node_is_logged_with_the_sow_fields(session, make_run) -> None:
    """Section 3 names these fields. The analysis layer is only as good as them."""
    graph = graph_of([setter("a", "x", 1)], [{"source": "t", "target": "a"}])
    run = await run_graph(session, make_run, graph)

    logged = await nodes_for(session, run.id)
    assert set(logged) == {"t", "a"}
    for record in logged.values():
        assert record.node_type
        assert record.status is NodeStatus.SUCCEEDED
        assert record.duration_ms is not None
        assert record.started_at is not None
        assert record.finished_at is not None
    assert logged["a"].output_summary is not None


async def test_condition_takes_one_branch(session, make_run) -> None:
    graph = graph_of(
        [
            condition("c", "{{ trigger.payload.n }}", "greater_than", 10),
            setter("big", "size", "big"),
            setter("small", "size", "small"),
        ],
        [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "big", "source_handle": "true"},
            {"source": "c", "target": "small", "source_handle": "false"},
        ],
    )
    run = await run_graph(session, make_run, graph, {"n": 42})

    logged = await nodes_for(session, run.id)
    assert logged["big"].status is NodeStatus.SUCCEEDED
    assert logged["small"].status is NodeStatus.SKIPPED


@pytest.mark.security
async def test_a_skipped_branch_is_recorded_not_omitted(session, make_run) -> None:
    """A missing row and a skipped row are indistinguishable in aggregate.

    If the untaken branch were simply absent, every Condition node would make
    its dead side look 100% healthy and per-node reliability would be wrong.
    """
    graph = graph_of(
        [
            condition("c", 1, "equals", 2),
            setter("yes", "v", 1),
            setter("no", "v", 2),
        ],
        [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "yes", "source_handle": "true"},
            {"source": "c", "target": "no", "source_handle": "false"},
        ],
    )
    run = await run_graph(session, make_run, graph)
    logged = await nodes_for(session, run.id)

    assert logged["yes"].status is NodeStatus.SKIPPED
    assert logged["yes"].status is not NodeStatus.SUCCEEDED
    assert logged["no"].status is NodeStatus.SUCCEEDED


async def test_a_failing_node_fails_the_run_and_says_why(session, make_run) -> None:
    graph = graph_of(
        [setter("a", "x", "{{ nodes.nonexistent.output }}")], [{"source": "t", "target": "a"}]
    )
    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.FAILED
    assert run.error and "nonexistent" in run.error
    logged = await nodes_for(session, run.id)
    assert logged["a"].status is NodeStatus.FAILED
    assert logged["a"].error


async def test_downstream_nodes_do_not_run_after_a_failure(session, make_run) -> None:
    graph = graph_of(
        [setter("a", "x", "{{ missing.thing }}"), setter("b", "y", 2)],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "b"}],
    )
    run = await run_graph(session, make_run, graph)

    logged = await nodes_for(session, run.id)
    assert run.status is RunStatus.FAILED
    assert "b" not in logged


# ---------------------------------------------------------------------------
# The event log — what makes cross-mode attach possible
# ---------------------------------------------------------------------------


@pytest.mark.security
async def test_events_are_persisted_with_a_gapless_sequence(session, make_run) -> None:
    """A caller attaching late replays from these. A gap is a lost event."""
    graph = graph_of([setter("a", "x", 1)], [{"source": "t", "target": "a"}])
    run = await run_graph(session, make_run, graph)

    events = await replay(session, run.id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "run.started"
    assert events[-1].type == "run.succeeded"


async def test_replay_after_a_sequence_returns_only_the_remainder(session, make_run) -> None:
    """This is what Last-Event-ID buys: resume, not restart."""
    graph = graph_of([setter("a", "x", 1)], [{"source": "t", "target": "a"}])
    run = await run_graph(session, make_run, graph)

    everything = await replay(session, run.id)
    tail = await replay(session, run.id, after=2)
    assert [e.seq for e in tail] == [e.seq for e in everything if e.seq > 2]


async def test_a_failed_run_still_ends_with_a_terminal_event(session, make_run) -> None:
    """Without one, a streaming client waits until its timeout."""
    graph = graph_of([setter("a", "x", "{{ nope.nope }}")], [{"source": "t", "target": "a"}])
    run = await run_graph(session, make_run, graph)

    events = await replay(session, run.id)
    assert events[-1].type == "run.failed"


async def test_progress_messages_reach_the_event_log(session, make_run) -> None:
    """Section 4's SSE contract has a `progress` field; Condition emits one."""
    graph = graph_of([condition("c", 1, "equals", 1)], [{"source": "t", "target": "c"}])
    run = await run_graph(session, make_run, graph)

    events = await replay(session, run.id)
    progress = [e for e in events if e.type == "node.progress"]
    assert progress and "progress" in progress[0].data
