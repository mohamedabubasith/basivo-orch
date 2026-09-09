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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.security.ratelimit import limiter
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.db import get_async_session
from basivo_orch.flows import service
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Flow, FlowVersion, Run, RunStatus, TriggerKind
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


class ChatAccepted(BaseModel):
    run_id: uuid.UUID
    status: RunStatus


class ChatReply(BaseModel):
    status: RunStatus
    reply: str = ""
    error: str | None = None


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
    session: AsyncSession = Depends(get_async_session),
) -> ChatAccepted:
    """Take a message and start a run. The answer is collected by polling."""
    flow, version, _ = await _published(session, flow_id, token)
    run, created = await service.create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.WEBHOOK,
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
    session: AsyncSession = Depends(get_async_session),
) -> ChatReply:
    """Where the reply is up to. Terminal states carry the words or the fault."""
    await _published(session, flow_id, token)
    run = await session.get(Run, run_id)
    if run is None or run.flow_id != flow_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    if run.status is RunStatus.SUCCEEDED:
        text = reply_text(run.output)
        return ChatReply(
            status=run.status,
            reply=text or "The flow finished without anything to say.",
        )
    if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
        # The flow's own error is not shown: it is written for the person who
        # built the flow, and can name repositories, models and credentials.
        return ChatReply(status=run.status, error="Something went wrong answering that.")
    return ChatReply(status=run.status)
