"""The shared tool and MCP library.

What matters here is the seam: a tool saved in one place has to reach an agent
in another workspace's flow never, reach an agent in its own always, and behave
exactly like an inline one when it gets there.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.flows.nodes.agent import AgentConfig, ToolDefinition
from basivo_orch.toolbox.models import McpConnection, Tool
from basivo_orch.toolbox.schemas import McpWrite, ToolWrite


def test_a_saved_tool_is_the_same_shape_the_node_already_understands():
    """The row is split into columns plus a JSON rest, and put back together it
    has to validate as the node's own ToolDefinition — otherwise a tool saved
    in the library fails on somebody else's run."""
    payload = ToolWrite(
        name="get_order",
        description="Look up an order by its number.",
        kind="http",
        url="https://api.example.com/orders/{{ tool.number }}",
        method="GET",
        input_schema={
            "type": "object",
            "properties": {"number": {"type": "string"}},
            "required": ["number"],
        },
    )
    row = payload.to_row()
    assert row["name"] == "get_order"

    rebuilt = ToolDefinition.model_validate(
        {
            "name": row["name"],
            "description": row["description"],
            "kind": row["kind"],
            **row["definition"],
        }
    )
    assert rebuilt.url.endswith("{{ tool.number }}")
    assert rebuilt.method == "GET"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"name": "x", "description": "d", "kind": "http"}, "needs a URL"),
        ({"name": "x", "description": "d", "kind": "code"}, "needs its code"),
    ],
)
def test_a_tool_missing_what_its_kind_needs_is_refused_at_save_time(payload, message):
    """The same rule the node enforces, applied where a person can still fix it."""
    with pytest.raises(ValueError, match=message):
        ToolWrite(**payload)


def test_an_mcp_server_must_be_reachable_over_http():
    with pytest.raises(ValueError, match="http"):
        McpWrite(name="local", url="stdio://run-me")


async def test_saved_tools_reach_the_agent_and_stay_scoped(session, organization):
    """The seam, twice over: this workspace's tool is loaded, another's is not."""
    from basivo_orch.auth.models import Organization
    from basivo_orch.flows.engine import Engine
    from basivo_orch.flows.graph import Graph
    from basivo_orch.flows.models import Run, RunStatus, TriggerKind

    stranger = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(stranger)
    await session.flush()

    mine = Tool(
        organization_id=organization.id,
        name="get_order",
        description="Look up an order.",
        kind="http",
        definition={"url": "https://api.example.com/o", "method": "GET", "headers": {}},
    )
    theirs = Tool(
        organization_id=stranger.id,
        name="their_secret",
        description="Not for us.",
        kind="http",
        definition={"url": "https://elsewhere.example.com", "method": "GET", "headers": {}},
    )
    server = McpConnection(
        organization_id=organization.id,
        name="docs",
        url="https://mcp.example.com/mcp",
        headers={},
        tools=[],
    )
    off = McpConnection(
        organization_id=organization.id,
        name="retired",
        url="https://old.example.com/mcp",
        headers={},
        tools=[],
        enabled=False,
    )
    session.add_all([mine, theirs, server, off])
    await session.commit()

    graph = Graph.model_validate(
        {
            "nodes": [{"id": "t", "type": "trigger.manual", "config": {}}],
            "edges": [],
        }
    )
    run = Run(
        flow_id=uuid.uuid4(),
        flow_version_id=uuid.uuid4(),
        organization_id=organization.id,
        trigger=TriggerKind.MANUAL,
        input={},
        status=RunStatus.QUEUED,
    )
    engine = Engine(session, run=run, graph=graph, redis_client=None)

    loaded = await engine._load_toolbox(
        [str(mine.id), str(theirs.id), "not-a-uuid"],
        [str(server.id), str(off.id)],
    )

    assert [tool["name"] for tool in loaded["tools"]] == ["get_order"], (
        "another workspace's tool reached this run"
    )
    # A disabled server is treated as missing: that is what the switch is for.
    assert [item["name"] for item in loaded["mcp_servers"]] == ["docs"]

    # And what came back is what the node consumes.
    config = AgentConfig(prompt="hi", model="m")
    merged = config.model_copy(
        update={"tools": [ToolDefinition.model_validate(item) for item in loaded["tools"]]}
    )
    assert merged.tools[0].name == "get_order"
