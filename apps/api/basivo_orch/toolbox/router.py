"""The tool and MCP library's HTTP surface.

Org-scoped through the same `require()` chokepoint as flows and credentials.
It carries the SKILL permissions rather than a pair of its own: a tool and a
skill are the same kind of thing to a workspace — something an agent is given,
managed by whoever manages agents — and inventing a second permission that is
granted to exactly the same roles buys nothing but another migration.

Listing returns whole rows. A tool definition is a few hundred bytes and the
picker wants to show what each one does; there is no body here to keep back.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.db import get_async_session
from basivo_orch.toolbox.models import McpConnection, Tool
from basivo_orch.toolbox.schemas import (
    McpRead,
    McpWrite,
    ToolRead,
    ToolWrite,
)

router = APIRouter(tags=["toolbox"])


async def _tool(session: AsyncSession, organization_id: uuid.UUID, tool_id: uuid.UUID) -> Tool:
    found = await session.execute(
        select(Tool).where(Tool.id == tool_id, Tool.organization_id == organization_id)
    )
    if row := found.scalar_one_or_none():
        return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such tool.")


async def _server(
    session: AsyncSession, organization_id: uuid.UUID, server_id: uuid.UUID
) -> McpConnection:
    found = await session.execute(
        select(McpConnection).where(
            McpConnection.id == server_id, McpConnection.organization_id == organization_id
        )
    )
    if row := found.scalar_one_or_none():
        return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such MCP server.")


def _taken(name: str, what: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"A {what} called {name!r} already exists here. The name is what the model calls it "
        "by, so two of them would make the choice a coin toss.",
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@router.get("/orgs/{organization_id}/tools", response_model=list[ToolRead])
async def list_tools(
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[Tool]:
    found = await session.execute(
        select(Tool).where(Tool.organization_id == context.organization_id).order_by(Tool.name)
    )
    return list(found.scalars())


@router.post("/orgs/{organization_id}/tools", response_model=ToolRead, status_code=201)
async def create_tool(
    payload: ToolWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> Tool:
    tool = Tool(
        organization_id=context.organization_id,
        created_by=context.user.id,
        **payload.to_row(),
    )
    session.add(tool)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _taken(payload.name, "tool") from None
    await session.refresh(tool)
    return tool


@router.patch("/orgs/{organization_id}/tools/{tool_id}", response_model=ToolRead)
async def update_tool(
    tool_id: uuid.UUID,
    payload: ToolWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> Tool:
    tool = await _tool(session, context.organization_id, tool_id)
    for field, value in payload.to_row().items():
        setattr(tool, field, value)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _taken(payload.name, "tool") from None
    await session.refresh(tool)
    return tool


@router.delete("/orgs/{organization_id}/tools/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.SKILL_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a tool. Agents referring to it stop being given it.

    Deliberately not blocked by references: a tool nobody should call any more
    must be removable now, and an agent that loses one carries on with the rest
    rather than failing. The run log records what it was given.
    """
    tool = await _tool(session, context.organization_id, tool_id)
    await session.delete(tool)
    await session.commit()


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@router.get("/orgs/{organization_id}/mcp-servers", response_model=list[McpRead])
async def list_servers(
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[McpConnection]:
    found = await session.execute(
        select(McpConnection)
        .where(McpConnection.organization_id == context.organization_id)
        .order_by(McpConnection.name)
    )
    return list(found.scalars())


@router.post("/orgs/{organization_id}/mcp-servers", response_model=McpRead, status_code=201)
async def create_server(
    payload: McpWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> McpConnection:
    server = McpConnection(
        organization_id=context.organization_id,
        created_by=context.user.id,
        **payload.model_dump(),
    )
    session.add(server)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _taken(payload.name, "server") from None
    await session.refresh(server)
    return server


@router.patch("/orgs/{organization_id}/mcp-servers/{server_id}", response_model=McpRead)
async def update_server(
    server_id: uuid.UUID,
    payload: McpWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> McpConnection:
    server = await _server(session, context.organization_id, server_id)
    for field, value in payload.model_dump().items():
        setattr(server, field, value)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _taken(payload.name, "server") from None
    await session.refresh(server)
    return server


@router.delete("/orgs/{organization_id}/mcp-servers/{server_id}", status_code=204)
async def delete_server(
    server_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.SKILL_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    server = await _server(session, context.organization_id, server_id)
    await session.delete(server)
    await session.commit()
