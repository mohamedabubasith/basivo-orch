"""Integration: real flows through the real engine, every node type covered.

These tests run whole graphs through `Engine.execute()` — real topological
execution, real SQLite rows, real `run_event` sequences, real subprocesses for
code, a mock HTTP transport for the network. The unit suites prove each node's
internals; this file proves the claims that only hold *between* nodes:

- **multi-agent handover** — agent B's prompt receives agent A's reply through
  `{{ input.text }}`, with both agents' tokens and steps recorded separately;
- **the whole Tier-1 zoo in one flow** — code output feeding a condition,
  branches firing and skipping, variables landing, HTTP called with templated
  data;
- **agent tools inside a run** — a code tool executed mid-run, its
  `tool.called`/`tool.result` steps persisted to the event log.

The last test is the enforcement of a standing rule (see CLAUDE.md): every
type in the node registry must appear in EXERCISED_NODE_TYPES, so registering
a node without integration coverage fails the suite by construction rather
than by review vigilance.

Model calls use pydantic-ai's FunctionModel — the loop, tools, usage and step
logging are all real; only the provider's wire call is substituted, because
CI has no API key. The genuinely-live path is `test_live_provider.py`,
opt-in via environment variables.
"""

from __future__ import annotations

import httpx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows import nodes as registry
from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import replay
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import NodeExecution, NodeStatus, RunStatus


async def run_graph(
    session: AsyncSession,
    make_run,
    graph: Graph,
    payload: dict | None = None,
    http: httpx.AsyncClient | None = None,
):
    run = await make_run(graph, payload)
    return await Engine(session, run=run, graph=graph, redis_client=None, http=http).execute()


async def nodes_for(session: AsyncSession, run_id) -> dict[str, NodeExecution]:
    result = await session.execute(select(NodeExecution).where(NodeExecution.run_id == run_id))
    return {row.node_id: row for row in result.scalars()}


# ---------------------------------------------------------------------------
# Multi-agent handover
# ---------------------------------------------------------------------------


async def test_two_agents_hand_over_through_input_text(session, make_run, monkeypatch):
    """The question asked directly: can one workflow run several agents, and
    does agent B actually receive agent A's reply? Proven by capture: B's fake
    model records the prompt the engine handed it."""
    received_by_b: list[str] = []

    def model_a(messages, info):
        return ModelResponse(parts=[TextPart(content="Draft: the sky is blue.")])

    def model_b(messages, info):
        # The first request part of the last message is B's user prompt.
        prompt = messages[-1].parts[0].content
        received_by_b.append(prompt)
        return ModelResponse(parts=[TextPart(content=f"Reviewed: {prompt}")])

    async def fake_build_model(config, ctx):
        return FunctionModel(model_a if ctx.node_id == "writer" else model_b)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent._build_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {"id": "writer", "type": "agent.llm", "config": {"prompt": "write"}},
                {
                    "id": "reviewer",
                    "type": "agent.llm",
                    # The handover contract, exactly as the inspector hints it.
                    "config": {"prompt": "Review this: {{ input.text }}"},
                },
            ],
            "edges": [
                {"source": "t", "target": "writer"},
                {"source": "writer", "target": "reviewer"},
            ],
        }
    )

    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.SUCCEEDED
    assert received_by_b == ["Review this: Draft: the sky is blue."]
    assert run.output["result"]["text"] == "Reviewed: Review this: Draft: the sky is blue."

    # Each agent's cost accounting is its own row, not a blended figure.
    executions = await nodes_for(session, run.id)
    assert executions["writer"].tokens_in and executions["writer"].tokens_out
    assert executions["reviewer"].tokens_in and executions["reviewer"].tokens_out

    # And the step log keeps the order: A's lifecycle completes before B's begins.
    events = await replay(session, run.id)
    agent_steps = [(e.data["node_id"], e.data["step"]) for e in events if e.type == "node.step"]
    writer_last = max(i for i, (node, _) in enumerate(agent_steps) if node == "writer")
    reviewer_first = min(i for i, (node, _) in enumerate(agent_steps) if node == "reviewer")
    assert writer_last < reviewer_first


async def test_an_agent_code_tool_executes_inside_a_run(session, make_run, monkeypatch):
    """Tools inside a real run: the model calls the user's own function, the
    subprocess actually executes, and the steps land in the persisted log."""
    calls = {"n": 0}

    def fake_model(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="stamp", args={"label": "x7"}, tool_call_id="t1")]
            )
        return ModelResponse(parts=[TextPart(content="stamped")])

    async def fake_build_model(config, ctx):
        return FunctionModel(fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent._build_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "agent",
                    "type": "agent.llm",
                    "config": {
                        "prompt": "go",
                        "tools": [
                            {
                                "name": "stamp",
                                "kind": "code",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {"label": {"type": "string"}},
                                },
                                "code": (
                                    "def main(data):\n"
                                    '    return {"stamped": data["args"]["label"].upper()}\n'
                                ),
                            }
                        ],
                    },
                },
            ],
            "edges": [{"source": "t", "target": "agent"}],
        }
    )

    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.SUCCEEDED
    events = await replay(session, run.id)
    steps = {e.data["step"]: e.data for e in events if e.type == "node.step"}
    assert steps["tool.called"]["arguments"] == {"label": "x7"}
    assert steps["tool.result"]["ok"] is True
    assert "X7" in steps["tool.result"]["result_preview"]


# ---------------------------------------------------------------------------
# The Tier-1 zoo, in one flow
# ---------------------------------------------------------------------------


async def test_every_utility_node_cooperates_in_one_flow(session, make_run, monkeypatch):
    """trigger → code → condition →(true) set → http, with each node consuming
    the previous one's output. The HTTP transport is mocked (CI is offline);
    the SSRF guard is patched out here because it does live DNS and has its
    own dedicated suite in test_safety.py."""
    monkeypatch.setattr("basivo_orch.flows.nodes.http.assert_public_url", lambda url: None)

    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"accepted": True})

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "compute",
                    "type": "code.python",
                    "config": {
                        "code": 'def main(data):\n    return {"n": data["input"]["n"] * 2}\n'
                    },
                },
                {
                    "id": "gate",
                    "type": "logic.condition",
                    "config": {
                        "comparisons": [
                            {"left": "{{ input.n }}", "operator": "greater_than", "right": 10}
                        ]
                    },
                },
                {
                    "id": "label",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "verdict", "value": "big"}]},
                },
                {
                    "id": "notify",
                    "type": "http.request",
                    "config": {
                        "url": "https://hooks.example.test/notify",
                        "method": "POST",
                        "body": {
                            "verdict": "{{ vars.verdict }}",
                            "n": "{{ nodes.compute.output.n }}",
                        },
                    },
                },
                {
                    "id": "small",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "verdict", "value": "small"}]},
                },
            ],
            "edges": [
                {"source": "t", "target": "compute"},
                {"source": "compute", "target": "gate"},
                {"source": "gate", "target": "label", "source_handle": "true"},
                {"source": "gate", "target": "small", "source_handle": "false"},
                {"source": "label", "target": "notify"},
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        run = await run_graph(session, make_run, graph, payload={"n": 6}, http=client)

    assert run.status is RunStatus.SUCCEEDED

    executions = await nodes_for(session, run.id)
    assert executions["compute"].status is NodeStatus.SUCCEEDED
    assert executions["gate"].status is NodeStatus.SUCCEEDED
    assert executions["label"].status is NodeStatus.SUCCEEDED
    assert executions["notify"].status is NodeStatus.SUCCEEDED
    # The branch not taken is a recorded skip, not an absence.
    assert executions["small"].status is NodeStatus.SKIPPED

    # The HTTP node sent the templated payload assembled from two other nodes.
    assert len(seen_requests) == 1
    import json

    body = json.loads(seen_requests[0].content)
    assert body == {"verdict": "big", "n": 12}


async def test_webhook_and_schedule_triggers_shape_their_payloads(session, make_run):
    webhook_graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.webhook", "config": {}},
                {
                    "id": "keep",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "city", "value": "{{ input.body.city }}"}]},
                },
            ],
            "edges": [{"source": "t", "target": "keep"}],
        }
    )
    run = await run_graph(session, make_run, webhook_graph, payload={"body": {"city": "Chennai"}})
    assert run.status is RunStatus.SUCCEEDED
    assert run.output["result"]["city"] == "Chennai"

    schedule_graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "t",
                    "type": "trigger.schedule",
                    "config": {"mode": "interval", "interval_seconds": 60},
                },
                {
                    "id": "keep",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "ok", "value": True}]},
                },
            ],
            "edges": [{"source": "t", "target": "keep"}],
        }
    )
    run = await run_graph(session, make_run, schedule_graph)
    assert run.status is RunStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# The rule, enforced
# ---------------------------------------------------------------------------

#: Every node type exercised by this file, through the real engine. Adding a
#: node to the registry without adding it here fails the next test — which is
#: the point: integration coverage for new nodes is a build requirement, not a
#: review request. See CLAUDE.md, "Adding a node type".
EXERCISED_NODE_TYPES = {
    "trigger.manual",
    "trigger.webhook",
    "trigger.schedule",
    "code.python",
    "logic.condition",
    "data.set",
    "http.request",
    "agent.llm",
}


def test_every_registered_node_type_has_integration_coverage():
    registered = set(registry.REGISTRY)
    uncovered = registered - EXERCISED_NODE_TYPES
    assert not uncovered, (
        f"Node type(s) {sorted(uncovered)} are registered but not exercised by "
        "the engine integration suite. Add a flow that runs them here and list "
        "them in EXERCISED_NODE_TYPES — see CLAUDE.md, 'Adding a node type'."
    )
