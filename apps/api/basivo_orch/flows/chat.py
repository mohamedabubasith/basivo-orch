"""The hosted chat window: three public endpoints and no API key.

A flow that starts with the Chat trigger is reachable by a person, not only by
a program. That is the whole point of the node: the alternative was telling
somebody to POST to the run endpoint with an API key and read a stream of
events, which is a fine way to integrate and a terrible way to have a
conversation.

The URL carries a token derived from the deployment's SECRET_KEY, exactly like
the Telegram hook secret: no column, no migration, nothing in the flow's
exported graph, and rotating SECRET_KEY invalidates every chat link at once.
A link is a capability — anyone holding it may talk to the flow — which is
what "publish a chat page" means, and why the token is compared in constant
time and every failure answers the same 404.

The page sends a message and then polls. Polling rather than a stream because
this endpoint is unauthenticated: a public SSE connection held open per
visitor is a cheap way for a stranger to occupy a worker, and a reply takes
seconds, not minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.security.ratelimit import limiter
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.db import get_async_session
from basivo_orch.flows import service
from basivo_orch.flows.events import replay
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import (
    Artifact,
    Flow,
    FlowVersion,
    NodeExecution,
    Run,
    RunStatus,
    TriggerKind,
)
from basivo_orch.flows.nodes import REGISTRY
from basivo_orch.flows.nodes.triggers import ChatTriggerConfig

router = APIRouter(prefix="/chat", tags=["chat"])

#: One message every three seconds per address, and a burst allowance for the
#: polling that follows each one. Generous for a person, useless for a script.
SEND_LIMIT = "20/minute"
POLL_LIMIT = "240/minute"

_NOT_FOUND = "No chat is published at this link."


def chat_token(flow_id: uuid.UUID) -> str:
    """The link's secret half. Derived, never stored."""
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"chat-page:{flow_id}".encode(), hashlib.sha256).hexdigest()[:32]


def chat_url(flow_id: uuid.UUID, *, base: str | None = None) -> str:
    """Where a person opens this flow's chat."""
    origin = (base or str(get_auth_settings().frontend_base_url)).rstrip("/")
    return f"{origin}/chat/{flow_id}/{chat_token(flow_id)}"


class ChatMessage(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=4000)
    #: Made by the page and kept in the browser, so a reload continues the
    #: same conversation and the agent's memory has something to key on.
    session_id: str = Field(min_length=1, max_length=64)


class ChatWindow(BaseModel):
    """What the page needs to draw itself before anyone has typed."""

    title: str
    greeting: str
    placeholder: str
    suggestions: list[str]
    #: Whether visitors see the steps behind an answer. The flow's author
    #: decides; see `ChatTriggerConfig.show_activity`.
    show_activity: bool = True


class ChatAccepted(BaseModel):
    run_id: uuid.UUID
    status: RunStatus


class ChatStep(BaseModel):
    """One thing that happened while the answer was being made.

    Deliberately thin. This is shown to strangers, so it carries what a person
    waiting is entitled to see — which step, whether it finished, how long it
    took, which model, which tools — and nothing that belongs to the flow's
    owner: no prompts, no outputs, no credentials, no repository names.
    """

    label: str
    kind: str
    status: str
    duration_ms: int | None = None
    detail: str = ""


class ChatAttachment(BaseModel):
    """A file the run made, addressed so the window can show it.

    The url is under the chat's own path and carries its token, which is what
    lets a stranger see a rendered video without an account and without the
    artifact route being opened to the world.
    """

    url: str
    kind: Literal["image", "video", "audio", "file"]
    filename: str
    content_type: str
    size_bytes: int


class ChatReply(BaseModel):
    status: RunStatus
    reply: str = ""
    error: str | None = None
    steps: list[ChatStep] = Field(default_factory=list)
    attachments: list[ChatAttachment] = Field(default_factory=list)


async def _published(
    session: AsyncSession, flow_id: uuid.UUID, token: str
) -> tuple[Flow, FlowVersion, ChatTriggerConfig]:
    """The flow behind a chat link, or the same 404 for every way of failing.

    A caller probing links must not be able to tell an unpublished flow from a
    flow that does not exist from a flow whose trigger is a schedule.
    """
    flow = await session.get(Flow, flow_id)
    if flow is None or flow.published_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    if not hmac.compare_digest(token, chat_token(flow_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    graph = Graph.model_validate(version.graph)
    trigger = next((node for node in graph.nodes if node.type == "trigger.chat"), None)
    if trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return flow, version, ChatTriggerConfig.model_validate(trigger.config)


def reply_text(output: dict[str, Any] | None) -> str:
    """The answer, out of whatever the last node returned.

    An agent returns `{"text": ...}`, a plain completion the same, and a flow
    ending in something else returns whatever it returns. A chat window has to
    show one thing, so this walks the usual shapes and gives up honestly
    rather than printing a dict at a customer.
    """
    result = (output or {}).get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "reply", "message", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # Several terminal nodes: the run output is keyed by node id, and any
        # of them may hold the words.
        for value in result.values():
            if isinstance(value, dict):
                nested = reply_text({"result": value})
                if nested:
                    return nested
    return ""


def _tool_said(data: dict[str, Any]) -> str:
    """One tool call, described rather than named.

    A visitor watching a support chat should see that it looked something up,
    not the internal name of the tool that did it, and never the engine behind
    it: that is a supplier we may change on a Tuesday.
    """
    if data.get("kind") == "search":
        query = str((data.get("arguments") or {}).get("query") or "").strip()
        return f'searched the web for "{query[:60]}"' if query else "searched the web"
    name = str(data.get("tool") or "a tool").replace("_", " ")
    return f"used {name}"


async def activity(session: AsyncSession, run_id: uuid.UUID) -> list[ChatStep]:
    """What the flow did, in the words a visitor can be shown.

    Two sources, because they answer different halves of "what is taking so
    long": the node executions say which step is running, and the model events
    say what the model did inside one — which tools it called, how long it
    thought, which model answered.
    """
    executions = (
        (
            await session.execute(
                select(NodeExecution)
                .where(NodeExecution.run_id == run_id)
                .order_by(NodeExecution.started_at)
            )
        )
        .scalars()
        .all()
    )

    # What the flow DID, in the words a visitor can be shown. Not which model
    # answered and not which search engine was asked: those are ours, they
    # change, and a chat window is not where a customer learns our suppliers.
    doing: dict[str, list[str]] = {}
    for event in await replay(session, run_id):
        data = event.data or {}
        node_id = str(data.get("node_id") or "")
        step = data.get("step")
        if step == "tool.called":
            doing.setdefault(node_id, []).append(_tool_said(data))
        elif step == "agent.handover" and (to := data.get("to")):
            doing.setdefault(node_id, []).append(f"handed over to {to}")
        elif step == "agent.delegating" and (to := data.get("to")):
            doing.setdefault(node_id, []).append(f"asked {to}")
        elif step == "search.results":
            doing.setdefault(node_id, []).append(
                f"searched the web, {data.get('count', 0)} results"
            )

    steps: list[ChatStep] = []
    for execution in executions:
        cls = REGISTRY.get(execution.node_type)
        # Named, not counted: "searched the web" is worth waiting for and
        # "3 tool calls" is not.
        detail = ", ".join(list(dict.fromkeys(doing.get(execution.node_id, [])))[:4])
        steps.append(
            ChatStep(
                label=execution.node_name or (cls.label if cls else execution.node_type),
                kind=cls.label if cls else execution.node_type,
                status=str(execution.status),
                duration_ms=execution.duration_ms,
                detail=detail,
            )
        )
    return steps


def _artifact_ids(value: Any, found: list[str]) -> None:
    """Every artifact id a run's output mentions, in the order written."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("artifact_id") and isinstance(item, str) and item:
                found.append(item)
            else:
                _artifact_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _artifact_ids(item, found)


def _kind(content_type: str) -> str:
    for prefix in ("image", "video", "audio"):
        if content_type.startswith(prefix):
            return prefix
    return "file"


async def attachments(
    session: AsyncSession, run: Run, *, flow_id: uuid.UUID, token: str
) -> list[ChatAttachment]:
    """The files this answer came with.

    Taken from the run's own output first — a flow that returns one video means
    that video, not the narration and the preview still it also wrote — and
    only failing that from everything the run saved. Nothing is looked up by
    id from the request, so a link holder cannot fish for other runs' files.
    """
    wanted: list[str] = []
    _artifact_ids(run.output, wanted)

    query = select(Artifact).where(Artifact.run_id == run.id)
    if wanted:
        query = query.where(Artifact.id.in_([uuid.UUID(item) for item in dict.fromkeys(wanted)]))
    rows = (await session.execute(query.order_by(Artifact.created_at))).scalars().all()

    base = f"/chat/{flow_id}/{token}/artifacts"
    return [
        ChatAttachment(
            url=f"{base}/{row.id}",
            kind=_kind(row.content_type),  # type: ignore[arg-type]
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
        )
        for row in rows[:4]
    ]


@router.get("/{flow_id}/{token}", response_model=ChatWindow)
async def window(
    flow_id: uuid.UUID,
    token: str,
    session: AsyncSession = Depends(get_async_session),
) -> ChatWindow:
    """What to draw. No message is sent, so nothing runs and nothing is billed."""
    _, _, config = await _published(session, flow_id, token)
    return ChatWindow(
        title=config.title,
        greeting=config.greeting,
        placeholder=config.placeholder,
        suggestions=config.suggestions,
        show_activity=config.show_activity,
    )


@router.post(
    "/{flow_id}/{token}", response_model=ChatAccepted, status_code=status.HTTP_202_ACCEPTED
)
@limiter.limit(SEND_LIMIT)
async def send(
    flow_id: uuid.UUID,
    token: str,
    message: ChatMessage,
    request: Request,
    # `response` is not decoration: the rate limiter writes its headers onto
    # it, and raises if the endpoint does not take one. Without it every send
    # was a 500, which reaches a browser as a CORS error because an unhandled
    # exception skips the CORS middleware. Found by running it, not by a test:
    # the suite has rate limiting switched off.
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ChatAccepted:
    """Take a message and start a run. The answer is collected by polling."""
    flow, version, _ = await _published(session, flow_id, token)
    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.CHAT,
        payload={
            "text": message.text,
            "session_id": message.session_id,
            # Enough to tell two visitors apart in a run log, and no more: a
            # chat page is not a place to collect people quietly.
            "visitor": {"ip": request.client.host if request.client else ""},
        },
    )
    if created:
        service.enqueue(run)
    return ChatAccepted(run_id=run.id, status=run.status)


@router.get("/{flow_id}/{token}/{run_id}", response_model=ChatReply)
@limiter.limit(POLL_LIMIT)
async def answer(
    flow_id: uuid.UUID,
    token: str,
    run_id: uuid.UUID,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> ChatReply:
    """Where the reply is up to, and what has happened so far.

    The steps come back on every poll, not only at the end: a person watching a
    blank window for twenty seconds wants to know the agent is calling a tool,
    and that is precisely when it is worth telling them.
    """
    _, _, config = await _published(session, flow_id, token)
    run = await session.get(Run, run_id)
    if run is None or run.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    steps = await activity(session, run_id) if config.show_activity else []
    if run.status is RunStatus.SUCCEEDED:
        text = reply_text(run.output)
        files = await attachments(session, run, flow_id=flow_id, token=token)
        return ChatReply(
            status=run.status,
            # A flow that answers with a video has said plenty. Only a run that
            # produced neither words nor a file needs explaining.
            reply=text or ("" if files else "The flow finished without anything to say."),
            steps=steps,
            attachments=files,
        )
    if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
        # The flow's own error is not shown: it is written for the person who
        # built the flow, and can name repositories, models and credentials.
        return ChatReply(
            status=run.status, error="Something went wrong answering that.", steps=steps
        )
    return ChatReply(status=run.status, steps=steps)


@router.get("/{flow_id}/{token}/artifacts/{artifact_id}")
@limiter.limit(POLL_LIMIT)
async def artifact(
    flow_id: uuid.UUID,
    token: str,
    artifact_id: uuid.UUID,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Serve one file a run of THIS flow produced.

    The token admits the caller and the run's flow id is checked against the
    link's: a chat page can show its own videos and nothing else, so the id in
    the URL is not a permission any more than it is on the authenticated
    route.
    """
    await _published(session, flow_id, token)
    row = await session.get(Artifact, artifact_id)
    if row is None or row.run_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    run = await session.get(Run, row.run_id)
    if run is None or run.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)

    return Response(
        content=row.data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{row.filename}"',
            # Immutable: an artifact's bytes never change once written.
            "Cache-Control": "private, max-age=86400, immutable",
            # The rest of this API is same-origin, and rightly so. A chat page
            # is the exception: the file exists to be shown in a page, the
            # page may be served from another origin (a separate console host,
            # an embedded window), and the deployment-wide `same-origin` policy
            # made every video a broken frame. The link's token is what admits
            # the reader, not the origin they read from.
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )
