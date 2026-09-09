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
from urllib.parse import parse_qsl

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
    Artifact,
    Flow,
    FlowVersion,
    NodeExecution,
    NodeStatus,
    Run,
    RunStatus,
    TriggerKind,
)
from basivo_orch.skills.models import Skill
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


async def test_an_agent_loads_a_skill_from_the_library_mid_run(
    session, organization, make_run, monkeypatch
):
    """Skills through the real store: catalogue in the prompt, body on demand.

    The assertion that matters is the negative one — the procedure is absent
    from the first model call. If it were pasted into the system prompt this
    test would still pass on "the agent followed it", which is why the prompt
    itself is inspected.
    """
    seen: list[list[str]] = []

    async def fake_build_model(_ctx, **_kwargs):
        def respond(messages):
            seen.append([str(m.content) for m in messages])
            if turn_number(messages) == 0:
                return tool_call("load_skill", {"name": "refund-policy"}, call_id="s1")
            return says("Refund approved under the 30-day rule.")

        return FakeChatModel(respond=respond)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    skill = Skill(
        organization_id=organization.id,
        name="refund-policy",
        description="Use when a customer asks for money back, including chargebacks.",
        instructions="# Refunds\n\nUnder 30 days, refund in full without asking.",
        resources=[{"name": "exceptions.md", "content": "Enterprise: ask legal."}],
    )
    session.add(skill)
    await session.commit()
    await session.refresh(skill)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "support",
                    "type": "agent.llm",
                    "config": {
                        "prompt": "The customer wants a refund.",
                        "skills": [str(skill.id)],
                    },
                },
            ],
            "edges": [{"source": "t", "target": "support"}],
        }
    )

    run = await run_graph(session, make_run, graph)
    assert run.status is RunStatus.SUCCEEDED
    assert run.output["result"]["text"] == "Refund approved under the 30-day rule."

    # Turn one: the catalogue, not the procedure.
    assert '"refund-policy"' in seen[0][0]
    assert "refund in full" not in " ".join(seen[0])
    # Turn two: the procedure, having been asked for.
    assert "refund in full" in " ".join(seen[1])
    # The bundled file rode along with neither.
    assert "ask legal" not in " ".join(seen[1])

    events = await replay(session, run.id)
    steps = [e.data.get("step") for e in events if e.type == "node.step"]
    assert steps.index("skill.offered") < steps.index("skill.loaded")

    # The library learns what earns its place.
    await session.refresh(skill)
    assert skill.load_count == 1


async def test_another_workspaces_skill_is_invisible_to_the_engine(
    session, organization, make_run, monkeypatch
):
    """The id is in the graph, so the tenant filter is the only thing stopping
    a copied flow from reading someone else's process."""
    seen: list[list[str]] = []

    async def fake_build_model(_ctx, **_kwargs):
        def respond(messages):
            seen.append([str(m.content) for m in messages])
            return says("ok")

        return FakeChatModel(respond=respond)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    intruder = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(intruder)
    await session.flush()
    theirs = Skill(
        organization_id=intruder.id,
        name="their-secret-process",
        description="Use when handling their most valuable accounts.",
        instructions="Never discount below 40%.",
    )
    session.add(theirs)
    await session.commit()
    await session.refresh(theirs)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "a",
                    "type": "agent.llm",
                    "config": {"prompt": "hello", "skills": [str(theirs.id)]},
                },
            ],
            "edges": [{"source": "t", "target": "a"}],
        }
    )

    run = await run_graph(session, make_run, graph)

    # The run survives — a missing skill is not a failure — but nothing of
    # theirs reaches the model, not even the name.
    assert run.status is RunStatus.SUCCEEDED
    assert "their-secret-process" not in " ".join(seen[0])
    assert "Never discount" not in " ".join(seen[0])
    events = await replay(session, run.id)
    missing = [e.data for e in events if e.data.get("step") == "skill.missing"]
    assert missing and missing[0]["found"] == 0


async def test_a_narrated_video_is_authored_to_the_voice_and_captioned(
    session, make_run, monkeypatch
):
    """The order that makes narration work, proven end to end.

    The script is written and spoken BEFORE the animation exists, the agent is
    given the real length and the spoken word times, and the caption layer is
    driven by the composition's own timeline. Each of those is asserted rather
    than assumed, because each one fails silently: the wrong order cuts the
    voice off, a CSS-animated caption renders as one frozen frame.
    """
    from basivo_orch.flows.nodes import speech as speech_module
    from basivo_orch.flows.nodes import video as video_module

    prompts: list[str] = []
    rendered: dict[str, object] = {}

    async def fake_speak(text, *, voice, speed):
        # Six words, one a second, so the assertions can be exact.
        words = [
            {"word": word, "start": float(index), "end": index + 0.9}
            for index, word in enumerate(text.split()[:6])
        ]
        return b"RIFFnarration", 6.0, words

    monkeypatch.setattr(speech_module, "speak", fake_speak)

    async def fake_build_model(_ctx, **_kwargs):
        def respond(messages):
            # The whole turn, so the assertions can look at the instructions
            # and at what was asked for in the same string.
            prompts.append(" ".join(str(message.content) for message in messages))
            # Which pass this is, read off the instructions rather than
            # counted: a short script is expanded, which spends an extra call.
            latest = prompts[-1]
            if "narration for short product videos" in latest:
                return says("Ship your workflows today. Nothing else needed.")
            if "storyboard" in latest.lower() and "JSON" in latest:
                return says(
                    json.dumps(
                        {
                            "title": "Ship it",
                            "creative_direction": "Three held words on a dark field.",
                            "palette": ["#0b1020"],
                            "scenes": [
                                {
                                    "purpose": purpose,
                                    "headline": headline,
                                    "supporting_text": "",
                                    "visual": "A word on a dark field",
                                    "motion": "fade in",
                                    "duration_weight": 1,
                                }
                                for purpose, headline in (
                                    ("open", "Visible"),
                                    ("middle", "Faster"),
                                    ("close", "Ship it"),
                                )
                            ],
                        }
                    )
                )
            return says(
                'import React from "react";\n'
                'import {AbsoluteFill, useCurrentFrame, interpolate} from "remotion";\n'
                "export default function Scene() {\n"
                "  const frame = useCurrentFrame();\n"
                "  const enter = interpolate(frame, [0, 12], [0, 1]);\n"
                "  return <AbsoluteFill style={{opacity: enter}}><h1>Visible</h1>"
                "<h2>Faster</h2><h3>Ship it</h3>"
                "</AbsoluteFill>;\n"
                "}\n"
            )

        return FakeChatModel(respond=respond)

    monkeypatch.setattr("basivo_orch.flows.nodes.models.build_chat_model", fake_build_model)

    # The renderer is substituted, because a real render is minutes of CPU and
    # lives in test_video.py behind its own flag. Everything around it is real:
    # the order of the passes, the job the renderer is handed, and the artifact
    # that comes out.
    async def fake_probe(job, *, frames, scale=0.25):
        rendered["probe_job"] = job
        return [b"frame" for _ in frames]

    monkeypatch.setattr(video_module, "probe", fake_probe)

    async def fake_review(frames, *, seconds):
        return []

    monkeypatch.setattr(video_module, "review_frames", lambda frames, *, seconds: [])

    async def fake_render(job):
        rendered["job"] = job
        return b"\x00\x00\x00\x18ftypmp42" + b"0" * 200, {"durationInFrames": 189}

    monkeypatch.setattr(video_module, "render", fake_render)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "promo",
                    "type": "video.ai",
                    "name": "Narrated promo",
                    "config": {
                        "brief": "A 4 second promo for a workflow tool.",
                        "duration_seconds": 4,
                        "narration": True,
                        "captions": True,
                        "save_preview": False,
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                    },
                },
            ],
            "edges": [{"source": "t", "target": "promo"}],
        }
    )

    run = await run_graph(session, make_run, graph)
    assert run.status is RunStatus.SUCCEEDED, run.error

    # The agent was asked for words first, and markup second.
    assert "narration for short product videos" in prompts[0]
    # A range, not just a ceiling: asked only for a maximum, models come in far
    # under it and the video ends in silence.
    assert "between 8 and 10 words" in prompts[0]
    # The composition pass knew the real length and when each word lands.
    # Found by content rather than by index: a script that comes back short is
    # expanded, and that spends a call the index would then be off by.
    composition_prompt = prompts[-1]
    assert "6 seconds long" in composition_prompt
    assert "0.0s Ship" in composition_prompt
    # And it was implementing an agreed plan rather than inventing one twice.
    assert "APPROVED STORYBOARD" in composition_prompt

    job = rendered["job"]
    # The voice is a file beside the composition, and the renderer plays it.
    assert job.assets["narration.wav"] == b"RIFFnarration"
    assert job.audio == "narration.wav"
    # Captions are the product's, carried as data rather than spliced into the
    # composition, so nothing the agent wrote can collide with them.
    assert job.captions and job.captions[0]["text"].startswith("Ship")
    assert "Audio" not in job.scene_tsx, "the composition must not bring its own voice"
    # The brief asked for 4 seconds and the voice is 6, so the video is as long
    # as the voice. The other way round cuts it off mid-word.
    assert job.duration_seconds == 6.4

    events = await replay(session, run.id)
    steps = [e.data.get("step") for e in events if e.type == "node.step"]
    assert steps.index("video.script") < steps.index("video.spoken") < steps.index("video.attempt")
    spoken = [e.data for e in events if e.data.get("step") == "video.spoken"][0]
    assert spoken["seconds"] == 6.0

    assert run.output["result"]["duration_seconds"] == 6.4
    assert run.output["result"]["narration_artifact_id"]


async def test_two_agent_nodes_hand_over_on_the_canvas(session, make_run, monkeypatch):
    """Handover between NODES, not sub-agents inside one node.

    The point of doing it this way: the colleague is a node on the canvas with
    its own model, tools, skills, memory and cost row, its turn is its own step
    in the run log, and who may hand to whom is an edge someone drew rather
    than a list buried in one node's configuration.

    Also asserted: the front desk's own default edge does NOT fire. An agent
    that transferred has not answered, and running the rest of the flow on a
    non-answer is the failure this routing exists to prevent.
    """
    seen: dict[str, list[str]] = {}

    async def fake_build_model(ctx, **_kwargs):
        def respond(messages):
            seen.setdefault(ctx.node_id, []).append(str(messages[-1].content))
            if ctx.node_id == "desk":
                return tool_call(
                    "transfer_to_refunds",
                    {"reason": "they want money back"},
                    call_id="h1",
                )
            return says("Refunded in full.")

        return FakeChatModel(respond=respond)

    # Patched where `agent.py` bound it, not where it is defined: the module
    # imports the name at import time, so patching the source has no effect.
    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "desk",
                    "type": "agent.llm",
                    "name": "Front desk",
                    "config": {"prompt": "{{ input.text }}", "purpose": "First contact"},
                },
                {
                    "id": "refunds",
                    "type": "agent.llm",
                    "name": "Refunds",
                    "config": {
                        "prompt": "{{ input.text }}",
                        "purpose": "Refunds, billing disputes and chargebacks",
                    },
                },
                {
                    "id": "shipping",
                    "type": "agent.llm",
                    "name": "Shipping",
                    "config": {"prompt": "{{ input.text }}", "purpose": "Late parcels"},
                },
                {
                    "id": "log",
                    "type": "data.set",
                    "name": "After answering",
                    "config": {"assignments": [{"name": "done", "value": "yes"}]},
                },
            ],
            "edges": [
                {"source": "t", "target": "desk"},
                {"source": "desk", "target": "refunds", "source_handle": "handover"},
                {"source": "desk", "target": "shipping", "source_handle": "handover"},
                {"source": "desk", "target": "log"},
            ],
        }
    )

    run = await run_graph(session, make_run, graph, payload={"text": "I want a refund"})
    assert run.status is RunStatus.SUCCEEDED, run.error

    # The desk was told who its colleagues are, and what each one handles.
    prompt = " ".join(seen["desk"])
    assert "I want a refund" in prompt

    # Refunds ran and received the request itself, so it answers the person's
    # question rather than the desk's parting words. The reason travels
    # separately, as handover_note, for a prompt that wants it.
    assert "refunds" in seen, "the chosen colleague never ran"
    assert "I want a refund" in " ".join(seen["refunds"])
    assert "they want money back" not in " ".join(seen["refunds"])

    # The two paths not taken did not run.
    executions = await nodes_for(session, run.id)
    assert executions["refunds"].status is NodeStatus.SUCCEEDED
    assert executions["shipping"].status is NodeStatus.SKIPPED, "a handover is not a committee"
    assert executions["log"].status is NodeStatus.SKIPPED, (
        "the default edge must not fire: the desk transferred rather than answered"
    )

    desk_output = executions["desk"].output_summary or {}
    assert (desk_output.get("preview") or {}).get("handover_to") == "refunds"

    events = await replay(session, run.id)
    handovers = [e.data for e in events if e.data.get("step") == "agent.handover"]
    assert handovers and handovers[0]["to"] == "Refunds"
    assert handovers[0]["reason"] == "they want money back"

    # And the run's answer is the colleague's, not the desk's.
    assert run.output["result"]["text"] == "Refunded in full."


async def conversation(session, organization, graph: Graph):
    """Many runs against ONE published flow, which is what a chat is.

    `make_run` builds a fresh flow per call — right for testing a graph, wrong
    for testing a conversation: the session is keyed by flow and chat, so a new
    flow each time would give every message its own memory and hide exactly the
    bug this suite is for.
    """
    flow = Flow(
        organization_id=organization.id,
        name="Studio bot",
        slug=f"bot-{uuid.uuid4().hex[:8]}",
    )
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph=graph.model_dump(mode="json"))
    session.add(version)
    await session.commit()

    async def send(payload: dict | None = None, http=None):
        run = Run(
            flow_id=flow.id,
            flow_version_id=version.id,
            organization_id=organization.id,
            trigger=TriggerKind.WEBHOOK,
            input={"payload": payload or {}},
            status=RunStatus.QUEUED,
        )
        session.add(run)
        await session.commit()
        return await Engine(session, run=run, graph=graph, redis_client=None, http=http).execute()

    return send


async def test_a_studio_conversation_from_photos_to_a_second_attempt(
    session, make_run, organization, monkeypatch
):
    """The whole product, as a conversation.

    Four updates, four runs, one job: a photo arrives, a second photo arrives,
    the operator asks for a video, and then presses a button asking for a
    different one. What this proves is the thing the architecture rests on —
    the loop lives in the session row, not in the graph, so each message is a
    short DAG and nothing waits on a human.

    The Bot API is faked. What matters is which calls were made, in what order,
    and that the second render was told about the first attempt.
    """
    from basivo_orch.flows.nodes.base import ResolvedCredential

    async def fake_resolve(self, credential_id):
        return ResolvedCredential(
            provider="telegram", api_key="bot-token", base_url=None, options={}
        )

    monkeypatch.setattr(Engine, "_resolve_credential", fake_resolve)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    calls: list[tuple[str, dict]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = dict(parse_qsl(request.content.decode(errors="replace")))
        calls.append((method, body))

        if method == "getFile":
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/file_7.jpg"}}
            )
        if "/file/bot" in str(request.url):
            return httpx.Response(200, content=png)
        if method in {"sendMessage", "editMessageText", "sendVideo", "sendPhoto"}:
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 900 + len(calls)}}
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.telegram.org" in request.url.host or "/bot" in request.url.path:
            return telegram(request)
        return httpx.Response(404)

    # --- the flow, as a studio would wire it -------------------------------
    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "tg",
                    "type": "trigger.telegram",
                    "config": {"credential_id": "c1"},
                },
                {
                    "id": "collect",
                    "type": "session.state",
                    "config": {
                        "action": "add_photo",
                        "chat_id": "{{ input.chat_id }}",
                        "artifact_id": "{{ input.photos.0.artifact_id }}",
                        "file_unique_id": "{{ input.photos.0.file_unique_id }}",
                    },
                },
                {
                    "id": "ack",
                    "type": "telegram.reply",
                    "config": {
                        "credential_id": "c1",
                        "action": "send",
                        "chat_id": "{{ input.chat_id }}",
                        "text": "Got it.",
                    },
                },
            ],
            "edges": [
                {"source": "tg", "target": "collect"},
                {"source": "collect", "target": "ack"},
            ],
        }
    )

    send = await conversation(session, organization, graph)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # --- update 1: a photo --------------------------------------------
        first = await send(_update_with_photo("u-aaa"), http=client)
        assert first.status is RunStatus.SUCCEEDED, first.error

        # --- update 2: a second, different photo ---------------------------
        second = await send(_update_with_photo("u-bbb"), http=client)
        assert second.status is RunStatus.SUCCEEDED, second.error

        # --- update 3: the SAME photo again (a forward, or a redelivery) ---
        third = await send(_update_with_photo("u-bbb"), http=client)
        assert third.status is RunStatus.SUCCEEDED, third.error

    executions = await nodes_for(session, third.id)
    collected = executions["collect"].output_summary["preview"]
    assert collected["photo_count"] == 2, "the same picture twice is one picture"
    assert collected["duplicate"] is True

    # Each photo really was fetched and stored, not just recorded.
    stored = (
        (await session.execute(select(Artifact).where(Artifact.organization_id == organization.id)))
        .scalars()
        .all()
    )
    assert len(stored) >= 2
    assert all(bytes(item.data).startswith(b"\x89PNG") for item in stored)

    # And the operator was answered every time.
    assert [name for name, _ in calls].count("sendMessage") == 3


async def test_a_second_render_is_refused_while_the_first_is_running(session, organization):
    """Two taps of Generate is one render.

    On a two-core box a second concurrent render does not mean two videos in
    the same time; it means both take twice as long and the operator, who saw
    nothing happen, taps again.
    """
    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "claim",
                    "type": "session.state",
                    "config": {"action": "lock", "chat_id": "7712"},
                },
            ],
            "edges": [{"source": "t", "target": "claim"}],
        }
    )

    send = await conversation(session, organization, graph)
    first = await send()
    second = await send()

    got_it = (await nodes_for(session, first.id))["claim"].output_summary["preview"]
    denied = (await nodes_for(session, second.id))["claim"].output_summary["preview"]

    assert got_it["acquired"] is True
    assert denied["acquired"] is False, "the second tap must not start a second render"
    assert denied["locked"] is True


async def test_forget_deletes_the_photographs_not_just_the_row(session, make_run, organization):
    """These are photographs of a real wedding. /forget has to mean it."""
    from basivo_orch.flows import bot_sessions

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    saved = Artifact(
        organization_id=organization.id,
        run_id=None,
        node_id="tg",
        filename="guest.png",
        content_type="image/png",
        data=png,
        size_bytes=len(png),
    )
    session.add(saved)
    await session.commit()

    flow_id = uuid.uuid4()
    await bot_sessions.apply(
        session,
        organization_id=organization.id,
        flow_id=flow_id,
        chat_id="7712",
        action="add_photo",
        fields={"artifact_id": str(saved.id), "file_unique_id": "u-1"},
    )
    result = await bot_sessions.apply(
        session,
        organization_id=organization.id,
        flow_id=flow_id,
        chat_id="7712",
        action="forget",
    )

    assert result["photo_count"] == 0
    assert result["deleted_files"] == 1
    assert await session.get(Artifact, saved.id) is None, "the picture is gone, not orphaned"


def _update_with_photo(unique: str) -> dict:
    return {
        "body": {
            "update_id": abs(hash(unique)) % 10**6,
            "message": {
                "message_id": 5,
                "from": {"id": 7712, "first_name": "Ravi"},
                "chat": {"id": 7712, "type": "private"},
                "photo": [{"file_id": f"f-{unique}", "file_unique_id": unique, "file_size": 90000}],
            },
        }
    }


# ---------------------------------------------------------------------------
# One model call, no agent
# ---------------------------------------------------------------------------


async def test_write_with_ai_feeds_its_json_to_the_next_node(session, make_run, monkeypatch):
    """The scheduled content pipeline in miniature: a plain generation writes
    the post, and the node after it templates a field straight out of it. Proves
    the JSON contract and the cost row over the real engine, not a stub."""

    def fake_model(messages):
        # Fenced, because that is what providers actually return.
        return says(
            '```json\n{"headline": "Otters hold hands", "body": "So they do not drift."}\n```',
            input_tokens=90,
            output_tokens=25,
        )

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.llm.build_chat_model", fake_build_model)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.schedule", "config": {"cron": "0 9 * * *"}},
                {
                    "id": "write",
                    "type": "llm.generate",
                    "config": {"prompt": "Write a post about otters.", "output": "json"},
                },
                {
                    "id": "headline",
                    "type": "data.set",
                    "config": {
                        "assignments": [{"name": "headline", "value": "{{ input.json.headline }}"}]
                    },
                },
            ],
            "edges": [
                {"source": "t", "target": "write"},
                {"source": "write", "target": "headline"},
            ],
        }
    )

    run = await run_graph(session, make_run, graph)

    assert run.status is RunStatus.SUCCEEDED
    assert run.output["result"]["headline"] == "Otters hold hands"

    # The cost row the run detail page reads, on this node's own execution.
    executions = await nodes_for(session, run.id)
    assert executions["write"].tokens_in == 90
    assert executions["write"].tokens_out == 25

    events = await replay(session, run.id)
    steps = {e.data["step"]: e.data for e in events if e.type == "node.step"}
    assert steps["llm.response"]["output"] == "json"


EXERCISED_NODE_TYPES = {
    "trigger.telegram",
    "telegram.reply",
    "session.state",
    "image.ai",
    "video.ai",
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
    "llm.generate",
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

    # The designer is a second model, called by the image node with the
    # copywriter's line already templated into its brief.
    briefs: list[str] = []

    def designer(messages):
        briefs.append(str(messages[-1].content))
        return says(
            "<html><body style='margin:0;width:400px;height:400px;"
            "background:#7857ff;color:#fff;font-family:sans-serif'>"
            f"<h1>{briefs[-1].splitlines()[-1]}</h1></body></html>"
        )

    async def fake_designer(ctx, **kwargs):
        return FakeChatModel(respond=designer)

    monkeypatch.setattr("basivo_orch.flows.nodes.models.build_chat_model", fake_designer)

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
                    "type": "image.ai",
                    "name": "Poster",
                    "config": {
                        "brief": "A poster that says:\n{{ nodes.copy.output.text }}",
                        "size": "custom",
                        "width": 400,
                        "height": 400,
                        "scale": 1,
                        "model": "m",
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


async def test_the_video_node_revises_until_the_composition_actually_shows_something(
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

    def composition(body: str) -> str:
        return (
            'import React from "react";\n'
            'import {AbsoluteFill, useCurrentFrame, interpolate} from "remotion";\n'
            "export default function Scene() {\n"
            "  const frame = useCurrentFrame();\n"
            "  const enter = interpolate(frame, [0, 12], [0, 1]);\n"
            f"  return <AbsoluteFill style={{{{opacity: enter}}}}>{body}</AbsoluteFill>;\n"
            "}\n"
        )

    # The first attempt animates from a value the element already has, so it
    # renders as an empty video: nothing errors and nothing is logged.
    blank = composition("<span />")
    good = composition("<h1>Visible</h1>")

    attempts: list[str] = []
    model_calls = 0

    def author(messages):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return says(
                '{"title":"Promo","creative_direction":"Bold",'
                '"palette":["#111111","#ffffff"],"scenes":['
                '{"purpose":"Open","headline":"Visible","supporting_text":"",'
                '"visual":"Type","motion":"Fade","duration_weight":1,"asset":""},'
                '{"purpose":"Close","headline":"Visible","supporting_text":"",'
                '"visual":"Type","motion":"Scale","duration_weight":1,"asset":""}]}'
            )
        attempts.append("turn")
        return says(blank if len(attempts) == 1 else good)

    async def fake_build(ctx, **kwargs):
        return FakeChatModel(respond=author)

    monkeypatch.setattr("basivo_orch.flows.nodes.models.build_chat_model", fake_build)
    monkeypatch.setattr("basivo_orch.flows.nodes.video.build_chat_model", fake_build, raising=False)

    # The frames are the node's own eyes. Here the first composition comes
    # back blank and the second does not.
    async def fake_probe(job, *, frames, scale=0.25):
        return [b"blank" if "Visible" not in job.scene_tsx else b"drawn" for _ in frames]

    monkeypatch.setattr(video_module, "probe", fake_probe)

    def fake_review(frames, *, seconds):
        return [] if frames and frames[0] == b"drawn" else ["Every frame is a flat colour."]

    monkeypatch.setattr(video_module, "review_frames", fake_review)

    async def fake_render(job):
        assert "Visible" in job.scene_tsx, "the blank composition was rendered anyway"
        return b"\x00\x00\x00\x18ftypmp42" + b"0" * 200, {"width": job.width}

    monkeypatch.setattr(video_module, "render", fake_render)

    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "gen",
                    "type": "video.ai",
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
    assert executions["gen"].tokens_in == 30
    assert executions["gen"].tokens_out == 15
    assert len(attempts) == 2, "the agent was not asked to revise"

    from basivo_orch.flows.models import Artifact

    stored = (
        (await session.execute(select(Artifact).where(Artifact.run_id == run.id))).scalars().all()
    )
    assert len(stored) == 1 and stored[0].content_type == "video/mp4"
