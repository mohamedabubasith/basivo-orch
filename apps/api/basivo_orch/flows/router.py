"""HTTP surface for flows.

Two routers, because there are two audiences with different credentials:

* `management_router` — the editor and dashboard. Session-authenticated,
  organisation-scoped, permission-checked, mounted under the versioned prefix.
* `external_router` — SOW section 4. API-key authenticated, mounted at the
  paths the SOW specifies (`/flows/{id}/run`, …), because those are a contract
  someone will paste into their backend.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.db import get_async_session
from basivo_orch.flows import nodes as node_registry
from basivo_orch.flows import service
from basivo_orch.flows.apikeys import ApiCaller, generate_key, require_api_key
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.graph import Graph, GraphError
from basivo_orch.flows.models import ApiKey, Flow, FlowVersion, Run, RunStatus, TriggerKind
from basivo_orch.flows.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    FlowCreate,
    FlowDetail,
    FlowRead,
    FlowUpdate,
    NodeTypeRead,
    RunAccepted,
    RunDetail,
    RunRead,
    RunRequest,
)
from basivo_orch.flows.streaming import SSE_HEADERS, event_stream

management_router = APIRouter(tags=["flows"])
external_router = APIRouter(prefix="/flows", tags=["flow execution"])


def get_redis(request: Request) -> RedisClient | None:
    return getattr(request.app.state, "redis", None)


def _graph_error(exc: GraphError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"detail": "This flow cannot run yet.", "problems": exc.problems},
    )


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------


@management_router.get("/nodes", response_model=list[NodeTypeRead])
async def list_node_types() -> list[dict[str, Any]]:
    """Every node type the engine can run.

    The editor builds its palette from this rather than a hard-coded list, so a
    node cannot appear in the UI that the engine would reject at run time.
    """
    return node_registry.palette()


# ---------------------------------------------------------------------------
# Flow management
# ---------------------------------------------------------------------------


@management_router.post(
    "/orgs/{organization_id}/flows",
    response_model=FlowDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_flow(
    payload: FlowCreate,
    context: OrgContext = Depends(require(Permission.FLOW_CREATE)),
    session: AsyncSession = Depends(get_async_session),
) -> FlowDetail:
    # A flow may be saved in a broken state — half-drawn graphs are the normal
    # condition of an editor. Validation is enforced at publish, which is when
    # it can actually be run.
    try:
        flow, version = await service.create_flow(
            session,
            organization_id=context.organization_id,
            user_id=context.user.id,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            graph=payload.graph,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return FlowDetail(
        **FlowRead.model_validate(flow).model_dump(),
        graph=Graph.model_validate(version.graph),
        version=version.version,
    )


@management_router.get("/orgs/{organization_id}/flows", response_model=list[FlowRead])
async def list_flows(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[Flow]:
    return await service.list_flows(
        session, organization_id=context.organization_id, limit=limit, offset=offset
    )


async def _load_flow(session: AsyncSession, organization_id: uuid.UUID, flow_id: uuid.UUID) -> Flow:
    flow = await service.get_flow(session, organization_id=organization_id, flow_id=flow_id)
    if flow is None:
        # 404 rather than 403 for a flow in another tenant, matching how the
        # auth layer treats organisations: a 403 would confirm the id exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such flow.")
    return flow


@management_router.get("/orgs/{organization_id}/flows/{flow_id}", response_model=FlowDetail)
async def read_flow(
    flow_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> FlowDetail:
    flow = await _load_flow(session, context.organization_id, flow_id)
    version = await service.latest_version(session, flow.id)
    return FlowDetail(
        **FlowRead.model_validate(flow).model_dump(),
        graph=Graph.model_validate(version.graph),
        version=version.version,
    )


@management_router.patch("/orgs/{organization_id}/flows/{flow_id}", response_model=FlowDetail)
async def update_flow(
    flow_id: uuid.UUID,
    payload: FlowUpdate,
    context: OrgContext = Depends(require(Permission.FLOW_UPDATE)),
    session: AsyncSession = Depends(get_async_session),
) -> FlowDetail:
    flow = await _load_flow(session, context.organization_id, flow_id)

    if payload.name is not None:
        flow.name = payload.name
    if payload.description is not None:
        flow.description = payload.description

    if payload.graph is not None:
        version = await service.save_version(
            session, flow=flow, graph=payload.graph, user_id=context.user.id
        )
    else:
        await session.commit()
        version = await service.latest_version(session, flow.id)

    return FlowDetail(
        **FlowRead.model_validate(flow).model_dump(),
        graph=Graph.model_validate(version.graph),
        version=version.version,
    )


@management_router.post("/orgs/{organization_id}/flows/{flow_id}/validate")
async def validate_flow(
    flow_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Check a flow without publishing it, so the editor can show problems."""
    flow = await _load_flow(session, context.organization_id, flow_id)
    version = await service.latest_version(session, flow.id)
    try:
        service.validate(Graph.model_validate(version.graph))
    except GraphError as exc:
        return {"valid": False, "problems": exc.problems}
    return {"valid": True, "problems": []}


@management_router.post("/orgs/{organization_id}/flows/{flow_id}/publish")
async def publish_flow(
    flow_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_PUBLISH)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    flow = await _load_flow(session, context.organization_id, flow_id)
    try:
        version = await service.publish(session, flow=flow, user_id=context.user.id)
    except GraphError as exc:
        raise _graph_error(exc) from exc
    return {
        "flow_id": str(flow.id),
        "version": version.version,
        "published_at": version.published_at,
        "run_url": f"/flows/{flow.id}/run",
        "stream_url": f"/flows/{flow.id}/run/stream",
    }


@management_router.delete(
    "/orgs/{organization_id}/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_flow(
    flow_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    flow = await _load_flow(session, context.organization_id, flow_id)
    await session.delete(flow)
    await session.commit()


# ---------------------------------------------------------------------------
# Runs (management side)
# ---------------------------------------------------------------------------


@management_router.post("/orgs/{organization_id}/flows/{flow_id}/run", response_model=RunDetail)
async def test_run(
    flow_id: uuid.UUID,
    payload: RunRequest,
    context: OrgContext = Depends(require(Permission.FLOW_RUN)),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> RunDetail:
    """Run the *latest* version, published or not — the editor's test button."""
    flow = await _load_flow(session, context.organization_id, flow_id)
    version = await service.latest_version(session, flow.id)

    try:
        service.validate(Graph.model_validate(version.graph))
    except GraphError as exc:
        raise _graph_error(exc) from exc

    run, _ = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.MANUAL,
        payload=payload.input,
        user_id=context.user.id,
    )
    await service.execute(
        session, run=run, graph=Graph.model_validate(version.graph), redis_client=redis_client
    )
    return await _run_detail(session, context.organization_id, run.id)


@management_router.get("/orgs/{organization_id}/runs", response_model=list[RunRead])
async def list_runs(
    flow_id: uuid.UUID | None = None,
    run_status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[Run]:
    return await service.list_runs(
        session,
        organization_id=context.organization_id,
        flow_id=flow_id,
        status=run_status,
        limit=limit,
        offset=offset,
    )


@management_router.get("/orgs/{organization_id}/runs/stats")
async def run_statistics(
    flow_id: uuid.UUID | None = None,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Roll-up for the analysis layer."""
    return await service.run_stats(
        session, organization_id=context.organization_id, flow_id=flow_id
    )


async def _run_detail(
    session: AsyncSession, organization_id: uuid.UUID, run_id: uuid.UUID
) -> RunDetail:
    run = await service.get_run(
        session, organization_id=organization_id, run_id=run_id, with_nodes=True
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    return RunDetail(
        **RunRead.model_validate(run).model_dump(),
        nodes=list(run.node_executions),
    )


@management_router.get("/orgs/{organization_id}/runs/{run_id}", response_model=RunDetail)
async def read_run(
    run_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> RunDetail:
    """A run and its full node-level log — SOW section 3."""
    return await _run_detail(session, context.organization_id, run_id)


@management_router.get("/orgs/{organization_id}/runs/{run_id}/stream")
async def stream_run_from_ui(
    run_id: uuid.UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    redis_client: RedisClient | None = Depends(get_redis),
) -> StreamingResponse:
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return StreamingResponse(
        event_stream(
            run_id,
            organization_id=context.organization_id,
            redis_client=redis_client,
            after=after,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@management_router.post(
    "/orgs/{organization_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    payload: ApiKeyCreate,
    context: OrgContext = Depends(require(Permission.APIKEY_CREATE)),
    session: AsyncSession = Depends(get_async_session),
) -> ApiKeyCreated:
    key, prefix, digest = generate_key()
    record = ApiKey(
        organization_id=context.organization_id,
        name=payload.name,
        prefix=prefix,
        key_hash=digest,
        created_by=context.user.id,
        expires_at=(
            datetime.now(UTC) + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        ),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    # The only time the key exists outside the caller's hands. Only the digest
    # was stored, so there is no endpoint that could ever return it again.
    return ApiKeyCreated(**ApiKeyRead.model_validate(record).model_dump(), key=key)


@management_router.get("/orgs/{organization_id}/api-keys", response_model=list[ApiKeyRead])
async def list_api_keys(
    context: OrgContext = Depends(require(Permission.APIKEY_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[ApiKey]:
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.organization_id == context.organization_id)
        .order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars())


@management_router.delete(
    "/orgs/{organization_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_api_key(
    key_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.APIKEY_REVOKE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.organization_id == context.organization_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such API key.")

    # Revoked, not deleted: the row is what lets an audit answer "which key
    # made that call last March".
    record.revoked_at = datetime.now(UTC)
    await session.commit()


# ---------------------------------------------------------------------------
# External execution — SOW section 4
# ---------------------------------------------------------------------------


async def _published(
    session: AsyncSession, caller: ApiCaller, flow_id: uuid.UUID
) -> tuple[Flow, FlowVersion, Graph]:
    """Resolve the published version of a flow for an external caller."""
    flow = await service.get_flow(session, organization_id=caller.organization_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such flow.")
    if flow.published_version_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This flow has not been published. Publish it before calling it.",
        )

    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The published version is missing.")

    # External callers always get the *published* graph, never the draft the
    # editor is midway through. That is the whole point of publishing.
    return flow, version, Graph.model_validate(version.graph)


@external_router.post("/{flow_id}/run")
async def run_flow(
    flow_id: uuid.UUID,
    payload: RunRequest,
    response: Response,
    mode: Annotated[
        Literal["sync", "async"],
        Query(description="sync blocks until the run finishes; async returns 202 immediately."),
    ] = "sync",
    prefer: Annotated[str | None, Header()] = None,
    caller: ApiCaller = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> Any:
    """Run a published flow.

    Both variants from section 4 live on one path, because they are the same
    operation with different waiting behaviour — splitting them into two URLs
    would mean two things to publish and two to keep in step.

    `?mode=async` or `Prefer: respond-async` returns 202 with a run id.
    """
    flow, version, graph = await _published(session, caller, flow_id)

    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.API,
        payload=payload.input,
        idempotency_key=payload.idempotency_key,
    )

    if not created:
        # Replay of an idempotent request. Report the existing run rather than
        # starting a second one.
        response.headers["Idempotent-Replay"] = "true"
        if run.status.is_terminal:
            return RunRead.model_validate(run)
        response.status_code = status.HTTP_202_ACCEPTED
        return _accepted(flow.id, run)

    wants_async = mode == "async" or (prefer or "").lower().replace(" ", "") == "respond-async"

    if wants_async:
        service.execute_detached(run.id, graph, redis_client)
        response.status_code = status.HTTP_202_ACCEPTED
        return _accepted(flow.id, run)

    await service.execute(session, run=run, graph=graph, redis_client=redis_client)
    await session.refresh(run)
    return RunRead.model_validate(run)


def _accepted(flow_id: uuid.UUID, run: Run) -> RunAccepted:
    return RunAccepted(
        run_id=run.id,
        status=run.status,
        poll_url=f"/flows/{flow_id}/runs/{run.id}",
        stream_url=f"/flows/{flow_id}/runs/{run.id}/stream",
    )


@external_router.post("/{flow_id}/run/stream")
async def run_flow_streaming(
    flow_id: uuid.UUID,
    payload: RunRequest,
    caller: ApiCaller = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> StreamingResponse:
    """Start a run and stream its progress as Server-Sent Events."""
    flow, version, graph = await _published(session, caller, flow_id)

    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.API,
        payload=payload.input,
        idempotency_key=payload.idempotency_key,
    )
    if created:
        # Detached, so the response can start streaming immediately. Running it
        # inline would mean the first event arrived only after the last one.
        service.execute_detached(run.id, graph, redis_client)

    return StreamingResponse(
        event_stream(run.id, organization_id=caller.organization_id, redis_client=redis_client),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-Id": str(run.id)},
    )


@external_router.get("/{flow_id}/runs/{run_id}", response_model=RunDetail)
async def poll_run(
    flow_id: uuid.UUID,
    run_id: uuid.UUID,
    caller: ApiCaller = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
) -> RunDetail:
    """Poll a run. For callers that cannot hold a connection open."""
    run = await service.get_run(
        session, organization_id=caller.organization_id, run_id=run_id, with_nodes=True
    )
    if run is None or run.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")
    return RunDetail(
        **RunRead.model_validate(run).model_dump(),
        nodes=list(run.node_executions),
    )


@external_router.get("/{flow_id}/runs/{run_id}/stream")
async def attach_to_run(
    flow_id: uuid.UUID,
    run_id: uuid.UUID,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    caller: ApiCaller = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> StreamingResponse:
    """Attach to a run already in progress.

    This is section 4's cross-mode requirement: start a run with the plain
    request/response call above, then attach here to watch the rest of it. It
    works because events are persisted with a sequence — the stream replays
    what already happened before following the live tail, so attaching late
    costs you nothing.
    """
    run = await service.get_run(session, organization_id=caller.organization_id, run_id=run_id)
    if run is None or run.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")

    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return StreamingResponse(
        event_stream(
            run_id,
            organization_id=caller.organization_id,
            redis_client=redis_client,
            after=after,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
