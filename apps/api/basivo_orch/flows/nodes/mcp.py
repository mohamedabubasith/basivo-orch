"""MCP servers an agent may call, and the tools they become.

Both agents in this product — the AI Agent node and the repair agent behind
Fix Code and Open PR — can be given MCP servers from the node's configuration.
The server is reached over HTTP (Streamable HTTP transport); command-line
servers are refused on purpose, since "run this command on the worker" from a
tenant's flow config is remote code execution with a friendlier name.

Two consumers, one definition:

* the builtin loop connects with the official client, lists the server's
  tools and wraps each as a LangChain tool;
* Claude Code gets the same servers as an `--mcp-config` document and calls
  them natively.

A credential picked on the server travels as `Authorization: Bearer <key>`,
so nobody pastes a token into a header field where the exported graph would
carry it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from pydantic import BaseModel, Field, field_validator

from basivo_orch.flows.nodes.base import NodeContext, NodeError

#: LangChain / OpenAI cap tool names at 64 characters.
MAX_TOOL_NAME = 64


class McpServer(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-zA-Z0-9_-]+$",
        title="Name",
        description="A short handle. Its tools appear to the agent as name__tool.",
    )
    url: str = Field(
        min_length=8,
        max_length=500,
        title="URL",
        description="The server's HTTP endpoint, for example https://mcp.example.com/mcp.",
    )
    credential_id: str = Field(
        default="",
        max_length=64,
        title="Credential",
        description="Optional. Sent as Authorization: Bearer <key>.",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        title="Extra headers",
        description="Only if the server needs something beyond the credential.",
    )
    tools: list[str] = Field(
        default_factory=list,
        max_length=100,
        title="Only these tools",
        description="Empty means every tool the server offers.",
    )

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                "MCP servers are reached over http(s). Command-line servers are not "
                "supported here; run one behind an HTTP bridge instead."
            )
        return value


async def resolve_headers(ctx: NodeContext, server: McpServer) -> dict[str, str]:
    """The request headers for one server, credential included."""
    headers = dict(server.headers)
    if server.credential_id:
        credential = await ctx.resolve_credential(server.credential_id)
        if credential is None:
            raise NodeError(
                f"MCP server {server.name!r}: its credential was not found in this workspace."
            )
        headers.setdefault("Authorization", f"Bearer {credential.api_key}")
    return headers


def tool_name(server: McpServer, tool: str) -> str:
    return f"{server.name}__{tool}"[:MAX_TOOL_NAME]


def result_text(result: Any) -> str:
    """A tool result as text the model can read: text blocks joined, anything
    else (images, resources) named rather than dropped silently."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'content')} omitted]")
    structured = _attr(result, "structured_content", "structuredContent")
    if structured and not parts:
        parts.append(json.dumps(structured, default=str))
    text = "\n".join(parts).strip()
    if _attr(result, "is_error", "isError"):
        return f"The tool reported an error: {text or 'no detail'}"
    return text or "(no output)"


def _attr(obj: Any, *names: str) -> Any:
    """The SDK renamed its fields to snake_case in 2.x; accept either spelling."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


@asynccontextmanager
async def mcp_toolset(
    ctx: NodeContext,
    servers: list[McpServer],
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[list[Any]]:
    """Connect to every server, yield their tools, disconnect afterwards.

    The sessions stay open for the whole agent run: a tool call is a message
    on an established session, not a fresh connection per call. Every failure
    to connect is a NodeError naming the server, because "the agent had fewer
    tools than the flow says" is not a thing a run log may leave implicit.

    `transport` exists for tests, which serve a real MCP server in-process.
    """
    if not servers:
        yield []
        return

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from basivo_orch.flows.nodes.agent_runtime import build_tool

    tools: list[Any] = []
    try:
        async with AsyncExitStack() as stack:
            for server in servers:
                headers = await resolve_headers(ctx, server)
                # One stack per server, handed to the run's stack only once the
                # handshake succeeded: a failed connection is closed here, on its
                # own, and reported as one sentence naming the server.
                own = AsyncExitStack()
                try:
                    # httpx2, not httpx: the SDK's transport is written against
                    # it (it is the SDK's own dependency), and its event-stream
                    # reader wants that package's response objects.
                    client = await own.enter_async_context(
                        httpx2.AsyncClient(
                            headers=headers,
                            timeout=httpx2.Timeout(30.0, read=300.0),
                            transport=transport,
                        )
                    )
                    read, write, *_ = await own.enter_async_context(
                        streamable_http_client(server.url, http_client=client)
                    )
                    session = await own.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listing = await session.list_tools()
                except Exception as exc:  # noqa: BLE001 — every failure becomes one sentence
                    try:
                        await own.aclose()
                    except Exception:  # noqa: BLE001, S110 — already failing
                        pass
                    leaf = _leaf(exc)
                    raise NodeError(
                        f"MCP server {server.name!r} at {server.url} could not be used: "
                        f"{type(leaf).__name__}: {str(leaf)[:300]}"
                    ) from exc
                await stack.enter_async_context(own)

                wanted = set(server.tools)
                offered = [t for t in listing.tools if not wanted or t.name in wanted]
                missing = sorted(wanted - {t.name for t in listing.tools})
                await ctx.step(
                    "mcp.connected",
                    {
                        "server": server.name,
                        "tools": [t.name for t in offered],
                        **({"not_offered": missing} if missing else {}),
                    },
                )
                for remote in offered:
                    tools.append(_wrap(ctx, session, server, remote, build_tool))
            yield tools
    except BaseExceptionGroup as group:
        # The SDK runs its transport in anyio task groups, which wrap anything
        # raised while they are open — including the agent's own NodeError —
        # in ExceptionGroups. The engine wants the exception, not the wrapping.
        raise _leaf(group) from group


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _wrap(ctx: NodeContext, session: Any, server: McpServer, remote: Any, build_tool: Any) -> Any:
    name = tool_name(server, remote.name)

    async def execute(**arguments: Any) -> str:
        await ctx.step(
            "mcp.call", {"server": server.name, "tool": remote.name, "arguments": arguments}
        )
        try:
            result = await session.call_tool(remote.name, arguments or None)
        except Exception as exc:  # noqa: BLE001 — the model gets the error, the run goes on
            text = f"The call failed: {type(exc).__name__}: {str(exc)[:300]}"
        else:
            text = result_text(result)
        await ctx.step(
            "mcp.result", {"server": server.name, "tool": remote.name, "preview": text[:500]}
        )
        return text

    schema = dict(
        _attr(remote, "input_schema", "inputSchema") or {"type": "object", "properties": {}}
    )
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return build_tool(
        name=name,
        description=(remote.description or f"{remote.name} on {server.name}")[:1024],
        input_schema=schema,
        execute=execute,
    )


async def claude_code_config(
    ctx: NodeContext, servers: list[McpServer]
) -> tuple[dict[str, Any], list[str]]:
    """The `--mcp-config` document for these servers, and the tool patterns
    that allow them. `mcp__<name>` allows a whole server; `mcp__<name>__<tool>`
    one tool of it."""
    config: dict[str, Any] = {}
    allowed: list[str] = []
    for server in servers:
        entry: dict[str, Any] = {"type": "http", "url": server.url}
        headers = await resolve_headers(ctx, server)
        if headers:
            entry["headers"] = headers
        config[server.name] = entry
        if server.tools:
            allowed.extend(f"mcp__{server.name}__{tool}" for tool in server.tools)
        else:
            allowed.append(f"mcp__{server.name}")
    return {"mcpServers": config}, allowed


def skills_prompt(skills: list[Any], *, budget_chars: int) -> str:
    """Workspace skills as a section of a system prompt, within a budget.

    Claude Code has no `load_skill` tool to call, so the procedures travel in
    the prompt itself. Whole skills only: a procedure cut mid-sentence is
    worse than one left out, and the ones that did not fit are named so the
    run log can say why the agent never followed them.
    """
    if not skills:
        return ""
    parts: list[str] = []
    spent = 0
    skipped: list[str] = []
    for skill in skills:
        body = f"### {skill.name}\n{skill.description}\n\n{skill.instructions}".strip()
        if spent + len(body) > budget_chars:
            skipped.append(skill.name)
            continue
        parts.append(body)
        spent += len(body)
    text = "## Skills to follow where they apply\n\n" + "\n\n".join(parts) if parts else ""
    if skipped:
        text += (
            "\n\n(Skills left out because they did not fit the budget: " + ", ".join(skipped) + ")"
        )
    return text
