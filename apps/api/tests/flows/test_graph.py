"""Graph validation: what the editor is allowed to save and publish."""

from __future__ import annotations

import pytest

from basivo_orch.flows import nodes as registry
from basivo_orch.flows.graph import Graph, GraphError, topological_order, validate_graph


def build(nodes: list[dict], edges: list[dict]) -> Graph:
    return Graph.model_validate({"nodes": nodes, "edges": edges})


def check(graph: Graph) -> None:
    validate_graph(graph, known_types=registry.REGISTRY)


TRIGGER = {"id": "t", "type": "trigger.manual", "config": {}}


def set_node(node_id: str) -> dict:
    return {
        "id": node_id,
        "type": "data.set",
        "config": {"assignments": [{"name": "x", "value": 1}]},
    }


def test_a_minimal_flow_is_valid() -> None:
    check(build([TRIGGER, set_node("a")], [{"source": "t", "target": "a"}]))


def test_a_flow_needs_a_trigger() -> None:
    with pytest.raises(GraphError) as caught:
        check(build([set_node("a")], []))
    assert any("trigger" in problem for problem in caught.value.problems)


def test_two_triggers_are_rejected() -> None:
    """Two entry points would make 'what started this run' ambiguous."""
    second = {"id": "t2", "type": "trigger.webhook", "config": {}}
    with pytest.raises(GraphError) as caught:
        check(build([TRIGGER, second, set_node("a")], [{"source": "t", "target": "a"}]))
    assert any("found 2" in problem for problem in caught.value.problems)


def test_a_cycle_is_reported_with_its_path() -> None:
    """Knowing a loop exists is not actionable; knowing where it runs is."""
    graph = build(
        [TRIGGER, set_node("a"), set_node("b")],
        [
            {"source": "t", "target": "a"},
            {"source": "a", "target": "b"},
            {"source": "b", "target": "a"},
        ],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    problem = caught.value.problems[0]
    assert "loop" in problem and "a" in problem and "b" in problem


def test_orphan_nodes_are_rejected() -> None:
    """A node the trigger cannot reach would never run, which is never intended."""
    graph = build([TRIGGER, set_node("a"), set_node("stranded")], [{"source": "t", "target": "a"}])
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("stranded" in problem for problem in caught.value.problems)


def test_unknown_node_type_is_rejected() -> None:
    graph = build(
        [TRIGGER, {"id": "x", "type": "capability.code_agent", "config": {}}],
        [{"source": "t", "target": "x"}],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("unknown type" in problem for problem in caught.value.problems)


def test_bad_node_config_is_rejected_at_save_time() -> None:
    """Catching this now is the difference between a red underline in the
    editor and a 3am failure when the webhook fires."""
    graph = build(
        [TRIGGER, {"id": "h", "type": "http.request", "config": {"method": "TELEPORT"}}],
        [{"source": "t", "target": "h"}],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("misconfigured" in problem for problem in caught.value.problems)


def test_every_problem_is_reported_at_once() -> None:
    """One save should underline every broken node, not just the first."""
    graph = build(
        [
            {"id": "a", "type": "nope.missing", "config": {}},
            {"id": "b", "type": "also.missing", "config": {}},
        ],
        [],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert len(caught.value.problems) >= 2


def test_a_trigger_may_not_have_inputs() -> None:
    graph = build(
        [TRIGGER, set_node("a")],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "t"}],
    )
    with pytest.raises(GraphError):
        check(graph)


def test_node_ids_must_be_addressable_in_templates() -> None:
    """Ids appear as `nodes.<id>.output`, so a dot would break the path."""
    with pytest.raises(ValueError):
        Graph.model_validate({"nodes": [{"id": "a.b", "type": "data.set"}], "edges": []})


def test_topological_order_is_deterministic() -> None:
    """Two runs of one graph must produce logs that can be diffed."""
    graph = build(
        [TRIGGER, set_node("b"), set_node("a"), set_node("c")],
        [
            {"source": "t", "target": "b"},
            {"source": "t", "target": "a"},
            {"source": "a", "target": "c"},
            {"source": "b", "target": "c"},
        ],
    )
    assert topological_order(graph) == topological_order(graph)
    order = topological_order(graph)
    assert order.index("t") < order.index("a") < order.index("c")


def test_every_registered_node_declares_a_unique_type() -> None:
    assert len(registry.REGISTRY) == len(registry.palette())


def test_the_palette_exposes_a_config_schema_for_every_node() -> None:
    """The editor renders forms from these; a missing one is an unusable node."""
    for entry in registry.palette():
        assert entry["config_schema"]["type"] == "object"
        assert entry["ports"]


def test_a_node_takes_its_input_from_one_connection() -> None:
    """Two upstreams would make 'what does this node run on' a guess. The
    canvas refuses the second connection; this is the same rule at save."""
    graph = build(
        [TRIGGER, set_node("a"), set_node("b")],
        [
            {"source": "t", "target": "a"},
            {"source": "t", "target": "b"},
            {"source": "a", "target": "b"},
        ],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("'b' has 2 inputs (a, t)" in problem for problem in caught.value.problems)


def test_the_same_source_cannot_feed_a_node_through_two_ports() -> None:
    condition = {
        "id": "c",
        "type": "logic.condition",
        "config": {"comparisons": [{"left": "1", "operator": "equals", "right": "1"}]},
    }
    graph = build(
        [TRIGGER, condition, set_node("a")],
        [
            {"source": "t", "target": "c"},
            {"source": "c", "target": "a", "source_handle": "true"},
            {"source": "c", "target": "a", "source_handle": "false"},
        ],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("'a' has 2 inputs" in problem for problem in caught.value.problems)


def test_an_edge_must_leave_a_port_the_node_has() -> None:
    graph = build(
        [TRIGGER, set_node("a"), set_node("b")],
        [
            {"source": "t", "target": "a"},
            {"source": "a", "target": "b", "source_handle": "true"},
        ],
    )
    with pytest.raises(GraphError) as caught:
        check(graph)
    assert any("no output port 'true'" in problem for problem in caught.value.problems)


def test_condition_ports_are_real_ports() -> None:
    condition = {
        "id": "c",
        "type": "logic.condition",
        "config": {"comparisons": [{"left": "1", "operator": "equals", "right": "1"}]},
    }
    check(
        build(
            [TRIGGER, condition, set_node("a"), set_node("b")],
            [
                {"source": "t", "target": "c"},
                {"source": "c", "target": "a", "source_handle": "true"},
                {"source": "c", "target": "b", "source_handle": "false"},
            ],
        )
    )
