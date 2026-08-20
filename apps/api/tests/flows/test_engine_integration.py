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

Model calls use a scripted fake chat model — the loop, tools, usage and step
logging are all real; only the provider's wire call is substituted, because
CI has no API key. The genuinely-live path is `test_live_provider.py`,
opt-in via environment variables.
"""

from __future__ import annotations

import json
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Organization
from basivo_orch.flows import nodes as registry
from basivo_orch.flows.engine import Engine
from basivo_orch.flows.events import replay
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import (
    AgentMemory,
    Flow,
    FlowVersion,
    NodeExecution,
    NodeStatus,
    Run,
    RunStatus,
    TriggerKind,
)
from tests.flows.fakes import FakeChatModel, says, tool_call, turn_number


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

    def model_a(messages):
        return says("Draft: the sky is blue.")

    def model_b(messages):
        # The first request part of the last message is B's user prompt.
        prompt = messages[-1].content
        received_by_b.append(prompt)
        return says(f"Reviewed: {prompt}")

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=model_a if ctx.node_id == "writer" else model_b)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

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

    def fake_model(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return tool_call("stamp", {"label": "x7"}, call_id="t1")
        return says("stamped")

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

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
# The ticket-and-fix pair, through the engine
# ---------------------------------------------------------------------------


async def test_ticket_then_autofix_flow(session, make_run, monkeypatch):
    """The product's headline path as an actual flow: a failing signal arrives,
    a ticket is raised with the error in it, and the autofix agent opens a PR —
    every repo mutation happening only after the fix is fully staged."""

    from basivo_orch.flows.nodes.base import ResolvedCredential

    async def fake_resolve(self, credential_id):
        return ResolvedCredential(provider="github", api_key="tok", base_url=None, options={})

    monkeypatch.setattr(Engine, "_resolve_credential", fake_resolve)

    def fix_model(messages):
        n = turn_number(messages)
        if n == 0:
            return tool_call("write_file", {"path": "app.py", "content": "fixed\n"}, call_id="w")
        return says("Replaced the broken handler.")

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=fix_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.gitops.build_chat_model", fake_build)

    requests: list[httpx.Request] = []

    def host(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(
                201, json={"html_url": "https://gh/acme/api/issues/9", "number": 9}
            )
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "s1"}})
        if "/contents/" in path and request.method == "GET":
            return httpx.Response(404, json={})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if "/contents/" in path and request.method == "PUT":
            return httpx.Response(201, json={})
        if path.endswith("/pulls"):
            return httpx.Response(
                201, json={"html_url": "https://gh/acme/api/pull/10", "number": 10}
            )
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "ticket",
                    "type": "git.ticket",
                    "config": {
                        "git_credential_id": "c1",
                        "repo": "acme/api",
                        "title": "Failure: {{ input.error }}",
                    },
                },
                {
                    "id": "fix",
                    "type": "git.autofix",
                    "config": {
                        "git_credential_id": "c1",
                        "repo": "acme/api",
                        "problem": "See ticket {{ input.url }}: broken handler",
                    },
                },
            ],
            "edges": [
                {"source": "t", "target": "ticket"},
                {"source": "ticket", "target": "fix"},
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(host)) as client:
        run = await run_graph(
            session, make_run, graph, payload={"error": "500s on /billing"}, http=client
        )

    assert run.status is RunStatus.SUCCEEDED, run.error
    result = run.output["result"]
    assert result["pr_url"] == "https://gh/acme/api/pull/10"

    # The ticket carried the error, and the autofix prompt carried the ticket.
    import json as _json

    issue_body = _json.loads(next(r for r in requests if r.url.path.endswith("/issues")).content)
    assert issue_body["title"] == "Failure: 500s on /billing"

    executions = await nodes_for(session, run.id)
    assert executions["ticket"].status is NodeStatus.SUCCEEDED
    assert executions["fix"].status is NodeStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Parallel branches
# ---------------------------------------------------------------------------


async def test_independent_branches_run_concurrently(session, make_run, monkeypatch):
    """Two agents hanging off one trigger are independent work.

    Measured by the clock, because that is how the bug was reported: a 59s
    branch and a 3m49s branch took 4m48s — the sum — instead of the longer of
    the two. Each fake model sleeps, so a sequential engine cannot pass: it
    would need 3 x DELAY, and the ceiling here is 2 x DELAY.
    """
    import asyncio
    import time

    DELAY = 0.4
    overlap: list[str] = []
    in_flight = {"n": 0, "peak": 0}

    def sleeper(node_id: str):
        async def model(messages):
            in_flight["n"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
            overlap.append(f"start:{node_id}")
            await asyncio.sleep(DELAY)
            overlap.append(f"end:{node_id}")
            in_flight["n"] -= 1
            return says(f"done {node_id}")

        return model

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=sleeper(ctx.node_id))

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {"id": "a1", "type": "agent.llm", "config": {"prompt": "one"}},
                {"id": "a2", "type": "agent.llm", "config": {"prompt": "two"}},
                # Downstream of a1 only: proves the wave scheduler still
                # respects dependencies while parallelising siblings.
                {"id": "a3", "type": "agent.llm", "config": {"prompt": "three"}},
            ],
            "edges": [
                {"source": "t", "target": "a1"},
                {"source": "t", "target": "a2"},
                {"source": "a1", "target": "a3"},
            ],
        }
    )

    started = time.monotonic()
    run = await run_graph(session, make_run, graph)
    elapsed = time.monotonic() - started

    assert run.status is RunStatus.SUCCEEDED
    # Three model calls at DELAY each. Sequential would be >= 3 x DELAY; the
    # dependency chain (a1 -> a3) makes 2 x DELAY the floor.
    assert elapsed < DELAY * 3, f"branches ran sequentially: {elapsed:.2f}s for 3 x {DELAY}s"
    assert in_flight["peak"] >= 2, "no two nodes were ever in flight at once"
    # a1 and a2 interleave; a3 begins only after a1 finished.
    assert overlap.index("start:a2") < overlap.index("end:a1")
    assert overlap.index("end:a1") < overlap.index("start:a3")

    executions = await nodes_for(session, run.id)
    assert {node_id: row.status for node_id, row in executions.items()} == {
        "t": NodeStatus.SUCCEEDED,
        "a1": NodeStatus.SUCCEEDED,
        "a2": NodeStatus.SUCCEEDED,
        "a3": NodeStatus.SUCCEEDED,
    }

    # Concurrent writers share one session and one event sequence: it must
    # still be gapless, or the live stream drops events.
    events = await replay(session, run.id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


async def test_a_failing_branch_lets_its_sibling_finish_and_fails_the_run(
    session, make_run, monkeypatch
):
    """A sibling's failure must not orphan work already in flight.

    The slow branch is mid-call when the fast one fails. It has to be allowed
    to finish and record its row — the tokens are already being billed — and
    the run still ends FAILED with the real error.
    """
    import asyncio

    def model_for(node_id: str):
        async def model(messages):
            if node_id == "boom":
                raise RuntimeError("provider exploded")
            await asyncio.sleep(0.3)
            return says("slow branch finished")

        return model

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=model_for(ctx.node_id))

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {"id": "boom", "type": "agent.llm", "config": {"prompt": "fail"}},
                {"id": "slow", "type": "agent.llm", "config": {"prompt": "slow"}},
            ],
            "edges": [
                {"source": "t", "target": "boom"},
                {"source": "t", "target": "slow"},
            ],
        }
    )

    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.FAILED
    assert "provider exploded" in run.error

    executions = await nodes_for(session, run.id)
    assert executions["boom"].status is NodeStatus.FAILED
    # The one that mattered: not left dangling in RUNNING.
    assert executions["slow"].status is NodeStatus.SUCCEEDED
    assert executions["slow"].finished_at is not None


# ---------------------------------------------------------------------------
# The rule, enforced
# ---------------------------------------------------------------------------


#: Every node type exercised by this file, through the real engine. Adding a
#: node to the registry without adding it here fails the next test — which is
#: the point: integration coverage for new nodes is a build requirement, not a
#: review request. See CLAUDE.md, "Adding a node type".
async def test_an_agent_remembers_across_two_runs_of_the_same_flow(
    session, organization, monkeypatch
):
    """Memory, end to end, through the real store.

    Two runs of *one* flow — a second run of a second flow would prove nothing,
    since memory is scoped to the flow. What the second model call receives is
    captured rather than inferred: the row existing in the table is not the
    same claim as the model having read it.

    The subject is rendered from the payload, so this is also the shape a
    support flow uses — one thread per issue, arriving on a webhook.
    """
    seen: list[list] = []

    async def fake_build_model(_ctx, **_kwargs):
        def respond(messages):
            seen.append([m.content for m in messages])
            return says("Widen the statement timeout.")

        return FakeChatModel(respond=respond)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.webhook", "config": {"require_signature": False}},
                {
                    "id": "support",
                    "type": "agent.llm",
                    "config": {
                        "prompt": "{{ input.body.text }}",
                        "memory": "conversation",
                        "memory_key": "issue-{{ input.body.issue }}",
                    },
                },
            ],
            "edges": [{"source": "t", "target": "support"}],
        }
    )

    flow = Flow(
        organization_id=organization.id, name="Support", slug=f"support-{uuid.uuid4().hex[:8]}"
    )
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
    session.add(version)
    await session.commit()

    async def fire(text: str) -> Run:
        run = Run(
            flow_id=flow.id,
            flow_version_id=version.id,
            organization_id=organization.id,
            trigger=TriggerKind.WEBHOOK,
            input={"payload": {"body": {"issue": 41, "text": text}}},
            status=RunStatus.QUEUED,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return await Engine(session, run=run, graph=graph, redis_client=None).execute()

    first = await fire("Postgres times out on the report page.")
    second = await fire("That did not help. What did you tell me to try?")

    assert first.status is RunStatus.SUCCEEDED
    assert second.status is RunStatus.SUCCEEDED

    # Run one saw one message. Run two saw the whole thread, new request last.
    assert seen[0] == ["Postgres times out on the report page."]
    assert seen[1] == [
        "Postgres times out on the report page.",
        "Widen the statement timeout.",
        "That did not help. What did you tell me to try?",
    ]

    # One row, keyed by the rendered subject, holding the windowed thread.
    rows = (
        (
            await session.execute(
                select(AgentMemory).where(AgentMemory.organization_id == organization.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].subject == "issue-41"
    assert rows[0].scope == f"{flow.id}:support"
    assert len(rows[0].turns) == 4

    # And the run log says what was recalled, which is what makes a surprising
    # answer debuggable months later.
    events = await replay(session, second.id)
    loaded = [e.data for e in events if e.data.get("step") == "memory.loaded"]
    assert loaded and loaded[0]["turns"] == 2


async def test_memory_is_not_readable_across_tenants(session, organization, monkeypatch):
    """Another workspace's row with the same scope and subject stays invisible.

    Scopes embed a flow id and so cannot collide in practice; the tenant filter
    is defence for the case where they somehow do, and it is only defence if it
    is in the query. Asserted by planting the collision.
    """
    seen: list[list] = []

    async def fake_build_model(_ctx, **_kwargs):
        def respond(messages):
            seen.append([m.content for m in messages])
            return says("ok")

        return FakeChatModel(respond=respond)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "a",
                    "type": "agent.llm",
                    "config": {"prompt": "hello", "memory": "conversation"},
                },
            ],
            "edges": [{"source": "t", "target": "a"}],
        }
    )

    flow = Flow(organization_id=organization.id, name="F", slug=f"f-{uuid.uuid4().hex[:8]}")
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
    session.add(version)

    intruder = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(intruder)
    await session.flush()
    session.add(
        AgentMemory(
            organization_id=intruder.id,
            scope=f"{flow.id}:a",
            subject="default",
            turns=[{"role": "user", "text": "the other tenant's secret"}],
        )
    )
    await session.commit()

    run = Run(
        flow_id=flow.id,
        flow_version_id=version.id,
        organization_id=organization.id,
        trigger=TriggerKind.MANUAL,
        input={"payload": {}},
        status=RunStatus.QUEUED,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    await Engine(session, run=run, graph=graph, redis_client=None).execute()

    assert seen[0] == ["hello"], "the other tenant's turn must not appear"


EXERCISED_NODE_TYPES = {
    "design.render",
    "video.render",
    "video.generate",
    "social.post",
    "git.comment",
    "trigger.manual",
    "trigger.webhook",
    "trigger.schedule",
    "code.python",
    "logic.condition",
    "data.set",
    "http.request",
    "agent.llm",
    "git.ticket",
    "git.autofix",
}


def test_every_registered_node_type_has_integration_coverage():
    registered = set(registry.REGISTRY)
    uncovered = registered - EXERCISED_NODE_TYPES
    assert not uncovered, (
        f"Node type(s) {sorted(uncovered)} are registered but not exercised by "
        "the engine integration suite. Add a flow that runs them here and list "
        "them in EXERCISED_NODE_TYPES — see CLAUDE.md, 'Adding a node type'."
    )


async def test_github_issue_with_a_screenshot_becomes_a_pr_and_a_reply(
    session, make_run, monkeypatch
):
    """The product's core loop, end to end, as one real flow.

    A GitHub `issues` webhook arrives carrying a screenshot; the condition
    admits only freshly opened issues; the repair agent reads the picture,
    stages a fix and opens a PR; and a comment goes back on the issue itself
    so the person who reported it hears about it where they are looking.

    Everything is real except the model's wire call and GitHub: the trigger
    payload is the shape GitHub actually sends, the image is fetched through
    the redirect, and the PR and comment are asserted on the wire.
    """
    from basivo_orch.flows.nodes.base import ResolvedCredential

    async def fake_resolve(self, credential_id):
        return ResolvedCredential(provider="github", api_key="tok", base_url=None, options={})

    monkeypatch.setattr(Engine, "_resolve_credential", fake_resolve)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    saw_image: list[str] = []

    def looking_model(messages):
        for message in messages:
            content = getattr(message, "content", None)
            if isinstance(content, list):
                saw_image.extend(
                    item.get("image_url", {}).get("url", "").split(";")[0].removeprefix("data:")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "image_url"
                )
        n = turn_number(messages)
        if n == 0:
            return tool_call(
                "write_file", {"path": "totals.py", "content": "TAX = 0.2\n"}, call_id="w"
            )
        return says("The screenshot showed tax at 0%. Set TAX back to 0.2.")

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=looking_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.gitops.build_chat_model", fake_build)

    requests: list[httpx.Request] = []

    def host(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path, method = request.url.path, request.method
        if request.url.host == "github.com" and "/user-attachments/" in path:
            return httpx.Response(
                302,
                headers={"location": "https://private-user-images.githubusercontent.com/1/s.png"},
            )
        if request.url.host == "private-user-images.githubusercontent.com":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base1"}})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"tree": [{"path": "totals.py", "type": "blob"}]})
        if "/contents/" in path and method == "GET":
            return httpx.Response(404, json={})
        if path.endswith("/git/refs") and method == "POST":
            return httpx.Response(201, json={})
        if "/contents/" in path and method == "PUT":
            return httpx.Response(201, json={})
        if path.endswith("/pulls") and method == "POST":
            return httpx.Response(
                201, json={"html_url": "https://github.com/acme/shop/pull/58", "number": 58}
            )
        if path.endswith("/comments") and method == "POST":
            return httpx.Response(201, json={"html_url": "https://gh/c/1", "id": 1})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "hook", "type": "trigger.webhook", "config": {}},
                {
                    "id": "opened",
                    "type": "logic.condition",
                    "config": {
                        "comparisons": [
                            {
                                "left": "{{ nodes.hook.output.body.action }}",
                                "operator": "equals",
                                "right": "opened",
                            }
                        ]
                    },
                },
                {
                    "id": "fix",
                    "type": "git.autofix",
                    "config": {
                        "git_credential_id": "c1",
                        "repo": "acme/shop",
                        "problem": (
                            "Issue #{{ nodes.hook.output.body.issue.number }}: "
                            "{{ nodes.hook.output.body.issue.title }}\n\n"
                            "{{ nodes.hook.output.body.issue.body }}"
                        ),
                    },
                },
                {
                    "id": "reply",
                    "type": "git.comment",
                    "config": {
                        "git_credential_id": "c1",
                        "repo": "acme/shop",
                        "issue_number": "{{ nodes.hook.output.body.issue.number }}",
                        "body": "Opened {{ nodes.fix.output.pr_url }} for this.",
                    },
                },
            ],
            "edges": [
                {"source": "hook", "target": "opened"},
                {"source": "opened", "target": "fix", "source_handle": "true"},
                {"source": "fix", "target": "reply"},
            ],
        }
    )

    # The payload shape GitHub actually delivers for an `issues` event.
    delivery = {
        "body": {
            "action": "opened",
            "issue": {
                "number": 31,
                "title": "Tax shows as 0% at checkout",
                "body": (
                    "Every order shows 0% tax since this morning.\n\n"
                    "![checkout](https://github.com/user-attachments/assets/abc-123)"
                ),
                "author_association": "OWNER",
            },
            "repository": {"full_name": "acme/shop"},
        },
        "headers": {"x-github-event": "issues"},
        "method": "POST",
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(host)) as client:
        run = await run_graph(session, make_run, graph, payload=delivery, http=client)

    assert run.status is RunStatus.SUCCEEDED, run.error

    # 1. The agent actually looked at the screenshot.
    assert set(saw_image) == {"image/png"}, "the model never received the issue's screenshot"

    # 2. A PR was opened — after the fix was staged, never before.
    mutations = [(r.method, r.url.path) for r in requests if r.method in ("POST", "PUT")]
    assert mutations == [
        ("POST", "/repos/acme/shop/git/refs"),
        ("PUT", "/repos/acme/shop/contents/totals.py"),
        ("POST", "/repos/acme/shop/pulls"),
        ("POST", "/repos/acme/shop/issues/31/comments"),
    ]

    # 3. The reporter was answered on their own issue, with the PR link.
    comment = json.loads(requests[-1].content)["body"]
    assert comment == "Opened https://github.com/acme/shop/pull/58 for this."

    executions = await nodes_for(session, run.id)
    assert executions["fix"].status is NodeStatus.SUCCEEDED
    assert executions["reply"].status is NodeStatus.SUCCEEDED


async def test_the_morning_poster_flow_renders_and_posts(session, make_run, monkeypatch):
    """The whole point of the poster feature, as one flow.

    A schedule fires, an agent writes the copy, a browser renders the poster
    with real fonts, and it is posted to Telegram with the image attached.
    The artifact is passed by id, never as bytes through the graph — the run
    log stays readable and the poster still arrives.
    """
    from basivo_orch.flows.nodes.base import ResolvedCredential

    async def fake_resolve(self, credential_id):
        return ResolvedCredential(
            provider="telegram", api_key="bot-token", base_url=None, options={}
        )

    monkeypatch.setattr(Engine, "_resolve_credential", fake_resolve)

    def copywriter(messages):
        return says("Ship the fix before standup")

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=copywriter)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build)

    sent: list[httpx.Request] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 5, "chat": {"username": "basivo"}}}
        )

    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "t",
                    "type": "trigger.schedule",
                    "config": {"mode": "cron", "cron": "0 7 * * *"},
                },
                {
                    "id": "copy",
                    "type": "agent.llm",
                    "name": "Copywriter",
                    "config": {"prompt": "Write today's headline.", "model": "m"},
                },
                {
                    "id": "poster",
                    "type": "design.render",
                    "name": "Poster",
                    "config": {
                        "html": (
                            "<html><body style='margin:0;width:400px;height:400px;"
                            "background:#7857ff;color:#fff;font-family:sans-serif'>"
                            "<h1>{{ nodes.copy.output.text }}</h1></body></html>"
                        ),
                        "size": "custom",
                        "width": 400,
                        "height": 400,
                        "scale": 1,
                        "wait_for_fonts": False,
                    },
                },
                {
                    "id": "post",
                    "type": "social.post",
                    "name": "Post it",
                    "config": {
                        "platform": "telegram",
                        "credential_id": "c1",
                        "target": "@basivo",
                        "text": "{{ nodes.copy.output.text }}",
                        "artifact_id": "{{ nodes.poster.output.artifact_id }}",
                    },
                },
            ],
            "edges": [
                {"source": "t", "target": "copy"},
                {"source": "copy", "target": "poster"},
                {"source": "poster", "target": "post"},
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        run = await run_graph(session, make_run, graph, http=client)

    assert run.status is RunStatus.SUCCEEDED, run.error

    executions = await nodes_for(session, run.id)
    assert executions["poster"].status is NodeStatus.SUCCEEDED
    assert executions["post"].status is NodeStatus.SUCCEEDED

    # The poster was stored as a file, and the real PNG reached Telegram.
    from basivo_orch.flows.models import Artifact

    artifacts = (
        (await session.execute(select(Artifact).where(Artifact.run_id == run.id))).scalars().all()
    )
    assert len(artifacts) == 1
    stored = artifacts[0]
    assert stored.content_type == "image/png"
    assert stored.data.startswith(b"\x89PNG\r\n\x1a\n")

    body = sent[0].content
    assert stored.data in body, "the rendered poster never reached Telegram"
    assert b"Ship the fix before standup" in body, "the agent's copy was not used as the caption"


async def test_a_video_node_takes_its_copy_from_an_agent_and_stores_the_file(
    session, make_run, monkeypatch
):
    """The video half of the same story: an agent writes the line, a template
    renders it, and the file lands as an artifact the posting node can attach.

    The renderer itself is substituted — a real render is minutes of CPU and
    belongs in `test_video.py` behind its own flag — but everything around it
    is real: the templating of variables from an upstream node, the duration
    guard, the artifact write, and the reference downstream nodes use.
    """
    from basivo_orch.flows.nodes import video as video_module

    captured: dict[str, object] = {}

    async def fake_render(html, *, variables, config):
        captured["variables"] = variables
        captured["template_applied"] = "Auto-fix shipped" in json.dumps(variables)
        return b"\x00\x00\x00\x18ftypmp42" + b"0" * 400, "ok"

    monkeypatch.setattr(video_module, "_render", fake_render)

    def copywriter(messages):
        return says("Auto-fix shipped")

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=copywriter)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "copy",
                    "type": "agent.llm",
                    "config": {"prompt": "Write a launch line.", "model": "m"},
                },
                {
                    "id": "clip",
                    "type": "video.render",
                    "name": "Promo",
                    "config": {
                        "template": "announcement",
                        "variables": '{"headline": "{{ nodes.copy.output.text }}"}',
                        "quality": "draft",
                    },
                },
            ],
            "edges": [
                {"source": "t", "target": "copy"},
                {"source": "copy", "target": "clip"},
            ],
        }
    )

    run = await run_graph(session, make_run, graph)
    assert run.status is RunStatus.SUCCEEDED, run.error

    # The agent's line reached the composition's variables.
    assert captured["variables"] == {"headline": "Auto-fix shipped"}

    from basivo_orch.flows.models import Artifact

    stored = (
        (await session.execute(select(Artifact).where(Artifact.run_id == run.id))).scalars().all()
    )
    assert len(stored) == 1
    assert stored[0].content_type == "video/mp4"
    assert stored[0].filename.endswith(".mp4")

    executions = await nodes_for(session, run.id)
    assert executions["clip"].status is NodeStatus.SUCCEEDED


async def test_the_video_generator_revises_until_the_composition_actually_shows_something(
    session, make_run, monkeypatch
):
    """The loop, as a flow: the agent's first attempt renders blank, the node
    tells it so, and the second attempt is accepted and rendered.

    This is the failure that made the node exist. A composition that animates
    `from` a value the element already has renders *successfully* as an empty
    video — nothing errors, nothing is logged, and the user gets six seconds of
    gradient. Catching it costs a second in a browser; not catching it costs a
    full render and the user's trust.
    """
    from basivo_orch.flows.nodes import video as video_module

    blank = (
        '<div id="stage" data-composition-id="p" data-duration="4" '
        'data-width="100" data-height="100"><h1 id="a">Hi</h1>'
        "<script>window.__timelines={p:1}</script></div>"
    )
    good = blank.replace('<h1 id="a">Hi</h1>', '<h1 id="a">Visible</h1>')

    attempts: list[str] = []

    def author(messages):
        attempts.append("turn")
        return says(blank if len(attempts) == 1 else good)

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=author)

    monkeypatch.setattr("basivo_orch.flows.nodes.models.build_chat_model", fake_build)
    monkeypatch.setattr("basivo_orch.flows.nodes.video.build_chat_model", fake_build, raising=False)

    # The browser probe is the node's own eyes; here it reports the first
    # composition as blank and the second as fine.
    async def fake_probe(html, *, width, height, duration):
        if "Visible" in html:
            return {1.0: ["Visible"]}, []
        return {1.0: []}, []

    monkeypatch.setattr(video_module, "probe_composition", fake_probe)

    async def fake_render(html, *, variables, config):
        assert "Visible" in html, "the blank composition was rendered anyway"
        return b"\x00\x00\x00\x18ftypmp42" + b"0" * 200, "ok"

    monkeypatch.setattr(video_module, "_render", fake_render)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "gen",
                    "type": "video.generate",
                    "name": "Make the promo",
                    "config": {
                        "brief": "A six second promo.",
                        "model": "m",
                        "duration_seconds": 4,
                        "max_attempts": 3,
                        "save_preview": False,
                    },
                },
            ],
            "edges": [{"source": "t", "target": "gen"}],
        }
    )

    run = await run_graph(session, make_run, graph)
    assert run.status is RunStatus.SUCCEEDED, run.error

    executions = await nodes_for(session, run.id)
    assert executions["gen"].status is NodeStatus.SUCCEEDED
    assert len(attempts) == 2, "the agent was not asked to revise"

    from basivo_orch.flows.models import Artifact

    stored = (
        (await session.execute(select(Artifact).where(Artifact.run_id == run.id))).scalars().all()
    )
    assert len(stored) == 1 and stored[0].content_type == "video/mp4"
