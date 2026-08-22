"""HTTP surface for flows.

Three routers, because there are three audiences with different credentials:

* `management_router` — the editor and dashboard. Session-authenticated,
  organisation-scoped, permission-checked, mounted under the versioned prefix.
* `external_router` — SOW section 4. API-key authenticated, mounted at the
  paths the SOW specifies (`/flows/{id}/run`, …), because those are a contract
  someone will paste into their backend.
* `hooks_router` — `/hooks/{flow_id}`, for senders that cannot hold an API
  key (a GitHub repository webhook has exactly one credential slot: its
  signing secret). The webhook trigger's secret is the authentication; see
  `basivo_orch.flows.webhooks`.
"""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.db import get_async_session
from basivo_orch.flows import analytics as analytics_module
from basivo_orch.flows import nodes as node_registry
from basivo_orch.flows import service
from basivo_orch.flows.apikeys import ApiCaller, generate_key, require_api_key
from basivo_orch.flows.events import RedisClient, replay
from basivo_orch.flows.graph import Graph, GraphError
from basivo_orch.flows.models import (
    ApiKey,
    Flow,
    FlowSchedule,
    FlowVersion,
    Run,
    RunStatus,
    TriggerKind,
)
from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig
from basivo_orch.flows.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    FlowCreate,
    FlowDetail,
    FlowRead,
    FlowSummary,
    FlowUpdate,
    NodeTypeRead,
    RunAccepted,
    RunDetail,
    RunRead,
    RunRequest,
    TelegramConnect,
    TemplateInstall,
)
from basivo_orch.flows.streaming import SSE_HEADERS, event_stream
from basivo_orch.flows.webhooks import (
    authenticate_hook,
    ensure_method_allowed,
    hook_idempotency_key,
    telegram_hook_secret,
    telegram_idempotency_key,
    wrap_hook_payload,
)

management_router = APIRouter(tags=["flows"])
external_router = APIRouter(prefix="/flows", tags=["flow execution"])
hooks_router = APIRouter(tags=["inbound hooks"])


def get_redis(request: Request) -> RedisClient | None:
    return getattr(request.app.state, "redis", None)


def _graph_error(exc: GraphError) -> HTTPException:
    # Not `{"detail": "...", "problems": [...]}`: FastAPI wraps whatever is
    # passed here inside its own top-level `{"detail": ...}` envelope, so a
    # key here also named "detail" produced a doubled `{"detail": {"detail":
    # ..., "problems": [...]}}` — a shape the frontend's error parser never
    # matched, so every failed publish or test run surfaced as the raw HTTP
    # phrase "Unprocessable Entity" instead of the actual reason.
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"message": "This flow cannot run yet.", "problems": exc.problems},
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


@management_router.get("/orgs/{organization_id}/flow-templates")
async def list_flow_templates(
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
) -> list[dict[str, Any]]:
    """Flows that arrive already wired, for someone who has not drawn one before."""
    from basivo_orch.flows.templates import TEMPLATES

    return [
        {
            "name": item.name,
            "title": item.title,
            "summary": item.summary,
            "detail": item.detail,
            "needs": list(item.needs),
            "tags": list(item.tags),
        }
        for item in TEMPLATES.values()
    ]


@management_router.post(
    "/orgs/{organization_id}/flow-templates/{template_name}",
    response_model=FlowDetail,
    status_code=status.HTTP_201_CREATED,
)
async def install_flow_template(
    template_name: str,
    payload: TemplateInstall,
    context: OrgContext = Depends(require(Permission.FLOW_CREATE)),
    session: AsyncSession = Depends(get_async_session),
) -> FlowDetail:
    """Create a flow from a template, with the credentials already filled in.

    The credentials are arguments rather than something to wire afterwards
    because the alternative is a flow that installs, looks finished, and fails
    on its first message with "pick a credential" on a node the person has
    never opened.
    """
    from basivo_orch.flows.templates import TEMPLATES

    template = TEMPLATES.get(template_name)
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No template called {template_name!r}.")

    # The agent node has to name the same provider as the credential it uses.
    llm_provider = "anthropic"
    if payload.llm_credential_id:
        from basivo_orch.credentials.models import Credential

        try:
            record = await session.get(Credential, uuid.UUID(payload.llm_credential_id))
        except ValueError:
            record = None
        if record is not None and record.organization_id == context.organization_id:
            llm_provider = record.provider

    graph = Graph.model_validate(
        template.build(
            telegram_credential_id=payload.telegram_credential_id,
            llm_credential_id=payload.llm_credential_id,
            llm_provider=llm_provider,
        )
    )
    try:
        flow, version = await service.create_flow(
            session,
            organization_id=context.organization_id,
            user_id=context.user.id,
            name=payload.name or template.title,
            slug=None,
            description=template.summary,
            graph=graph,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return FlowDetail(
        **FlowRead.model_validate(flow).model_dump(),
        graph=Graph.model_validate(version.graph),
        version=version.version,
    )


@management_router.post("/orgs/{organization_id}/flows/{flow_id}/telegram/connect")
async def connect_telegram_bot(
    flow_id: uuid.UUID,
    payload: TelegramConnect,
    context: OrgContext = Depends(require(Permission.FLOW_UPDATE)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Point a bot at this flow.

    Two calls to Telegram and no configuration for the operator to keep in
    sync. `getMe` proves the token before anything is saved — a typo caught at
    paste time is worth a great deal more than a bot that silently never
    answers — and `setWebhook` registers the URL along with a secret derived
    from this deployment's key, so nothing about the secret has to be typed,
    stored or exported.
    """
    flow = await _load_flow(session, context.organization_id, flow_id)

    from basivo_orch.auth.settings import get_settings as get_auth_settings
    from basivo_orch.credentials.crypto import decrypt
    from basivo_orch.credentials.models import Credential

    try:
        credential = await session.get(Credential, uuid.UUID(payload.credential_id))
    except ValueError:
        credential = None
    if (
        credential is None
        or credential.organization_id != context.organization_id
        or credential.provider != "telegram"
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Pick a Telegram credential holding the bot's token.",
        )

    base = (credential.base_url or "https://api.telegram.org").rstrip("/")
    api = f"{base}/bot{decrypt(credential.secret_encrypted)}"
    hook_url = f"{str(get_auth_settings().public_base_url).rstrip('/')}/hooks/{flow.id}"

    async with httpx.AsyncClient(timeout=20) as http:
        me = (await http.get(f"{api}/getMe")).json()
        if not me.get("ok"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Telegram rejected that token. Check it with BotFather and paste it again.",
            )
        registered = (
            await http.post(
                f"{api}/setWebhook",
                data={
                    "url": hook_url,
                    "secret_token": telegram_hook_secret(flow.id),
                    # Everything else a bot can receive is noise for this flow,
                    # and each one would be a run.
                    "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
                    "drop_pending_updates": payload.drop_pending,
                },
            )
        ).json()

    if not registered.get("ok"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Telegram would not accept the webhook: {registered.get('description')}. "
            "The URL has to be https and publicly reachable.",
        )

    bot = me["result"]
    return {
        "username": bot.get("username"),
        "name": bot.get("first_name"),
        "webhook": hook_url,
        # Said plainly, because it is the difference between a bot that works
        # and one that answers nothing: a flow has to be published before
        # Telegram's deliveries have anything to run.
        "published": flow.published_version_id is not None,
    }


@management_router.get("/orgs/{organization_id}/flows", response_model=list[FlowSummary])
async def list_flows(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[FlowSummary]:
    """The list, with what the list actually needs.

    Name and slug alone left the page unable to answer the questions it is
    opened with — what starts this, how big is it, did the last run pass — so
    each row carries them, gathered in a fixed number of queries rather than
    one per flow.
    """
    flows = await service.list_flows(
        session, organization_id=context.organization_id, limit=limit, offset=offset
    )
    summaries = await service.summarise_flows(session, flows)
    return [
        FlowSummary(**FlowRead.model_validate(flow).model_dump(), **summaries[flow.id])
        for flow in flows
    ]


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
    schedule = await session.get(FlowSchedule, flow.id)
    return FlowDetail(
        **FlowRead.model_validate(flow).model_dump(),
        graph=Graph.model_validate(version.graph),
        version=version.version,
        next_run_at=schedule.next_run_at if schedule else None,
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
        # Refreshed, and not for tidiness. `Flow.updated_at` is
        # `onupdate=func.now()`, so after an UPDATE its value lives in the
        # database and the ORM marks it expired — `expire_on_commit=False` does
        # not help, because the attribute was never loaded with the new value.
        # Serialising it then triggers a lazy SELECT from pydantic's sync
        # attribute access, which is a MissingGreenlet and a 500. The
        # graph-saving path above escapes it only by setting `updated_at` in
        # Python, which is easy to mistake for this path being fine too.
        await session.refresh(flow)
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


@management_router.post("/orgs/{organization_id}/flows/{flow_id}/run")
async def test_run(
    flow_id: uuid.UUID,
    payload: RunRequest,
    response: Response,
    mode: Annotated[
        Literal["sync", "async"],
        Query(
            description=(
                "sync blocks until the run finishes; async returns 202 with a run id "
                "to poll. The editor uses async — an agent flow can run for minutes, "
                "and a held-open request is a spinner with no progress and a proxy "
                "timeout waiting to happen."
            )
        ),
    ] = "sync",
    context: OrgContext = Depends(require(Permission.FLOW_RUN)),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> Any:
    """Run the *latest* version, published or not — the editor's test button."""
    flow = await _load_flow(session, context.organization_id, flow_id)
    version = await service.latest_version(session, flow.id)
    graph = Graph.model_validate(version.graph)

    try:
        service.validate(graph)
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

    if mode == "async":
        service.enqueue(run)
        response.status_code = status.HTTP_202_ACCEPTED
        return _accepted(flow.id, run)

    await service.execute(session, run=run, graph=graph, redis_client=redis_client)
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


@management_router.get("/orgs/{organization_id}/analytics")
async def flow_analytics(
    flow_id: uuid.UUID | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """The analysis layer of SOW section 3.

    Latency attribution, retry rescues, clustered failures and branches that
    never fire — the questions a run list cannot answer.
    """
    return await analytics_module.analytics(
        session, organization_id=context.organization_id, flow_id=flow_id, days=days
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


@management_router.get("/orgs/{organization_id}/artifacts/{artifact_id}")
async def read_artifact(
    artifact_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Serve a file a run produced — the poster, so it can be looked at.

    Session-authenticated and tenant-scoped like everything else here: a
    rendered poster can carry unreleased copy, and an id in a URL is not a
    permission. Inline rather than as a download, because the point is to see
    it on the run page.
    """
    from basivo_orch.flows.models import Artifact

    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.organization_id != context.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file.")

    return Response(
        content=artifact.data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{artifact.filename}"',
            # Immutable: an artifact's bytes never change once written.
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


@management_router.get("/orgs/{organization_id}/runs/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
    context: OrgContext = Depends(require(Permission.RUN_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """The full event log for one run, in order.

    The stream endpoint answers "what is happening"; this answers "what
    happened", a different question with a different shape. A finished run has
    no live tail to attach to, and opening an SSE connection just to read
    history would make the client reimplement ordering and termination to
    render a page that will never change again.

    This is where per-step agent detail surfaces: every model turn, tool call,
    token count and cost is a `node.step` event, ordered by the same gapless
    sequence the live stream uses — see `basivo_orch/flows/nodes/agent.py`.
    """
    # Ownership check first: without it, any member of any org could read any
    # run's events by guessing an id.
    if (
        await service.get_run(session, organization_id=context.organization_id, run_id=run_id)
        is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run.")

    events = await replay(session, run_id, after=after, limit=limit)
    return {
        "events": [
            {"seq": event.seq, "type": event.type, "data": event.data, "at": event.created_at}
            for event in events
        ],
        "next_after": events[-1].seq if events else after,
    }


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


def _verify_webhook_secret(graph: Graph, presented: str | None) -> None:
    """The webhook trigger's edge check — before a run row exists.

    The API key already authenticates *an* external caller; this authenticates
    *the* caller the flow's author expected. The two differ the moment a key
    leaks into a CI log or a partner's config: the secret is a second factor
    the author can rotate per flow without reissuing the workspace's key.
    Compared constant-time — a secret whose check leaks its prefix through
    timing is a secret with a countdown.
    """
    trigger = next((node for node in graph.nodes if node.type == "trigger.webhook"), None)
    if trigger is None:
        return
    config = WebhookTriggerConfig.model_validate(trigger.config)
    if not config.require_signature:
        return
    if not presented or not hmac.compare_digest(presented, config.secret):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "This flow requires the X-Webhook-Secret header, and it did not match.",
        )


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
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
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
    _verify_webhook_secret(graph, x_webhook_secret)

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
        service.enqueue(run)
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
    x_webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
    caller: ApiCaller = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> StreamingResponse:
    """Start a run and stream its progress as Server-Sent Events."""
    flow, version, graph = await _published(session, caller, flow_id)
    _verify_webhook_secret(graph, x_webhook_secret)

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
        service.enqueue(run)

    return StreamingResponse(
        event_stream(run.id, organization_id=caller.organization_id, redis_client=redis_client),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-Id": str(run.id)},
    )


_HOOK_404 = "No webhook is listening at this URL."


@hooks_router.api_route(
    "/hooks/{flow_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    response_model=RunAccepted,
)
async def inbound_hook(
    flow_id: uuid.UUID,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_async_session),
    redis_client: RedisClient | None = Depends(get_redis),
) -> RunAccepted:
    """Receive a raw webhook delivery and start the flow. No API key.

    Paste this URL into GitHub's or GitLab's webhook settings (or anything
    else that can POST) with the trigger's secret, and the sender's own
    authentication — GitHub's body signature, GitLab's token header — admits
    it. Always 202: webhook senders time out fast and only want the delivery
    acknowledged, and both hosts show this response body in their delivery
    log, so the run id lands somewhere a debugging human will actually look.

    Every failure before authentication is the same generic 404 — an
    unauthenticated caller probing URLs learns nothing about which flows
    exist, are published, or how they are configured.
    """
    flow = await session.get(Flow, flow_id)
    if flow is None or flow.published_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _HOOK_404)
    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _HOOK_404)
    graph = Graph.model_validate(version.graph)
    trigger = next(
        (node for node in graph.nodes if node.type in {"trigger.webhook", "trigger.telegram"}),
        None,
    )
    if trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _HOOK_404)

    raw_body = await request.body()
    telegram = trigger.type == "trigger.telegram"
    if telegram:
        # A bot's secret is generated when the bot is connected, not typed by
        # the person: Telegram only ever sends the value given to setWebhook,
        # so there is nothing for a studio owner to invent or keep in sync.
        config = WebhookTriggerConfig(
            require_signature=True,
            secret=telegram_hook_secret(flow.id),
            methods=["POST"],
        )
    else:
        config = WebhookTriggerConfig.model_validate(trigger.config)

    authenticate_hook(config, raw_body=raw_body, headers=request.headers)
    ensure_method_allowed(config, request.method)

    payload = wrap_hook_payload(
        method=request.method,
        headers=request.headers,
        query=request.query_params,
        raw_body=raw_body,
    )
    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.WEBHOOK,
        payload=payload,
        idempotency_key=(telegram_idempotency_key(payload.get("body")) if telegram else None)
        or hook_idempotency_key(request.headers),
    )
    if created:
        service.enqueue(run)
    else:
        # A redelivered webhook (same delivery UUID) reports the original run
        # rather than fixing the same issue twice.
        response.headers["Idempotent-Replay"] = "true"
    response.status_code = status.HTTP_202_ACCEPTED
    return _accepted(flow.id, run)


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
