"""MCP servers as agent tools, against a real MCP server served in-process.

The server is the reference SDK's own, mounted on an httpx ASGI transport; the
client is the same code the nodes use. What is asserted is the contract the
editor promises: tools appear as <server>__<tool>, calls go through, every
connection and call lands on the run log, and a server that cannot be reached
fails the run by name rather than shrinking the tool list in silence.
"""

from __future__ import annotations

import uuid

import httpx
import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from basivo_orch.flows.nodes.base import NodeContext, NodeError, ResolvedCredential
from basivo_orch.flows.nodes.mcp import (
    McpServer,
    claude_code_config,
    mcp_toolset,
    skills_prompt,
)
from basivo_orch.flows.nodes.skills import LoadedSkill


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))


def make_context(recorder: _Recorder) -> NodeContext:
    async def resolve_credential(credential_id: str):
        if credential_id == "cred-mcp":
            return ResolvedCredential(
                provider="mcp", api_key="tok-secret", base_url=None, options={}
            )
        return None

    async def progress(message: str) -> None:
        pass

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="agent",
        node_name="Agent",
        attempt=1,
        input=None,
        outputs={},
        variables={},
        trigger={},
        progress=progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=httpx.AsyncClient(),
    )


def docs_server() -> MCPServer:
    server = MCPServer("docs")

    @server.tool()
    def lookup(term: str) -> str:
        """Look up a term in the documentation."""
        return f"{term}: a documented thing"

    @server.tool()
    def count(items: list[str]) -> int:
        """Count items."""
        return len(items)

    return server


def asgi(server: MCPServer, *, json_response: bool = False) -> httpx2.ASGITransport:
    """The server as an in-process transport. Event-stream responses by
    default, which is what real servers send; JSON on request."""
    app = server.streamable_http_app(
        json_response=json_response,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return httpx2.ASGITransport(app=app)


def test_only_http_servers_are_accepted():
    with pytest.raises(ValidationError, match="http"):
        McpServer(name="local", url="npx -y some-server")
    assert McpServer(name="ok", url="https://mcp.example.com/mcp").tools == []


async def test_tools_are_listed_named_and_callable():
    server = docs_server()
    recorder = _Recorder()
    transport = asgi(server)  # builds the app, and with it the session manager
    async with server.session_manager.run():
        async with mcp_toolset(
            make_context(recorder),
            [McpServer(name="docs", url="http://mcp.test/mcp")],
            transport=transport,
        ) as tools:
            assert sorted(t.name for t in tools) == ["docs__count", "docs__lookup"]
            lookup = next(t for t in tools if t.name == "docs__lookup")
            assert await lookup.ainvoke({"term": "webhook"}) == "webhook: a documented thing"

    kinds = [kind for kind, _ in recorder.steps]
    assert kinds == ["mcp.connected", "mcp.call", "mcp.result"]
    connected = recorder.steps[0][1]
    assert connected == {"server": "docs", "tools": ["count", "lookup"]} or connected == {
        "server": "docs",
        "tools": ["lookup", "count"],
    }
    assert recorder.steps[1][1]["arguments"] == {"term": "webhook"}


async def test_the_tool_list_can_be_narrowed_and_a_missing_name_is_reported():
    server = docs_server()
    recorder = _Recorder()
    transport = asgi(server, json_response=True)
    async with server.session_manager.run():
        async with mcp_toolset(
            make_context(recorder),
            [McpServer(name="docs", url="http://mcp.test/mcp", tools=["lookup", "nope"])],
            transport=transport,
        ) as tools:
            assert [t.name for t in tools] == ["docs__lookup"]
    assert recorder.steps[0][1]["not_offered"] == ["nope"]


async def test_the_credential_travels_as_a_bearer_header():
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(500, text="boom")

    recorder = _Recorder()
    with pytest.raises(NodeError, match="MCP server 'docs' at https://mcp.test/mcp could not"):
        async with mcp_toolset(
            make_context(recorder),
            [McpServer(name="docs", url="https://mcp.test/mcp", credential_id="cred-mcp")],
            transport=httpx2.MockTransport(handler),
        ):
            pass
    assert seen and seen[0].headers["authorization"] == "Bearer tok-secret"


async def test_a_missing_credential_is_named():
    recorder = _Recorder()
    with pytest.raises(NodeError, match="MCP server 'docs': its credential was not found"):
        async with mcp_toolset(
            make_context(recorder),
            [McpServer(name="docs", url="https://mcp.test/mcp", credential_id="gone")],
        ):
            pass


async def test_an_error_raised_while_connected_comes_out_as_itself():
    """anyio wraps what the agent raises in ExceptionGroups; the engine must
    still see a NodeError, with its message and retry flag intact."""
    server = docs_server()
    transport = asgi(server)
    async with server.session_manager.run():
        with pytest.raises(NodeError, match="agent changed no files") as raised:
            async with mcp_toolset(
                make_context(_Recorder()),
                [McpServer(name="docs", url="http://mcp.test/mcp")],
                transport=transport,
            ):
                raise NodeError("The agent changed no files.", retryable=False)
    assert raised.value.retryable is False


async def test_no_servers_means_no_tools_and_no_connections():
    async with mcp_toolset(make_context(_Recorder()), []) as tools:
        assert tools == []


async def test_claude_code_gets_the_same_servers_as_a_config_document():
    config, allowed = await claude_code_config(
        make_context(_Recorder()),
        [
            McpServer(name="docs", url="https://mcp.test/mcp", credential_id="cred-mcp"),
            McpServer(name="jira", url="https://jira.test/mcp", tools=["search", "comment"]),
        ],
    )
    assert config == {
        "mcpServers": {
            "docs": {
                "type": "http",
                "url": "https://mcp.test/mcp",
                "headers": {"Authorization": "Bearer tok-secret"},
            },
            "jira": {"type": "http", "url": "https://jira.test/mcp"},
        }
    }
    assert allowed == ["mcp__docs", "mcp__jira__search", "mcp__jira__comment"]


def test_skills_ride_whole_in_the_prompt_within_the_budget():
    big = LoadedSkill(id="1", name="release", description="How we release.", instructions="x" * 500)
    small = LoadedSkill(id="2", name="style", description="House style.", instructions="Use tabs.")
    text = skills_prompt([big, small], budget_chars=200)
    assert "### style" in text and "Use tabs." in text
    assert "### release" not in text
    assert "did not fit the budget: release" in text
    assert skills_prompt([], budget_chars=100) == ""
