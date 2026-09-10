"""The public chat page: its link, its 404s, and the answer it hands back.

These endpoints are the only ones in the product a stranger can reach without
an API key, so most of what is asserted here is what they refuse.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import select

from basivo_orch.flows import service
from basivo_orch.flows.chat import (
    ChatMessage,
    answer,
    chat_token,
    chat_url,
    reply_text,
    send,
    window,
)
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Run, RunStatus, TriggerKind
from basivo_orch.flows.nodes.triggers import ChatTriggerConfig

CHAT_GRAPH = {
    "nodes": [
        {
            "id": "chat",
            "type": "trigger.chat",
            "name": "Chat",
            "config": {
                "title": "Support",
                "greeting": "What can I help with?",
                "placeholder": "Ask about your order",
                "suggestions": ["Where is my order?", "  ", "Refund policy"],
            },
        },
        {
            "id": "agent",
            "type": "agent.llm",
            "name": "Desk",
            "config": {"prompt": "{{ input.text }}", "model": "m"},
        },
    ],
    "edges": [{"source": "chat", "target": "agent"}],
}


class _Client:
    host = "203.0.113.7"


class _Request:
    """Enough of a request for the rate limiter and the visitor note."""

    client = _Client()
    headers: dict[str, str] = {}
    scope: dict[str, object] = {"type": "http"}


async def published(session, organization, graph: dict | None = None):
    flow, _ = await service.create_flow(
        session,
        organization_id=organization.id,
        user_id=None,
        name=f"Chat flow {uuid.uuid4().hex[:6]}",
        slug=None,
        description=None,
        graph=Graph.model_validate(graph or CHAT_GRAPH),
    )
    await service.publish(session, flow=flow, user_id=None)
    await session.commit()
    return flow


def test_the_token_is_derived_and_the_link_contains_it():
    """No column, no migration, and nothing in an exported graph. Rotating the
    deployment's SECRET_KEY invalidates every published chat link at once."""
    flow_id = uuid.uuid4()
    token = chat_token(flow_id)
    assert len(token) == 32
    assert token == chat_token(flow_id), "the same flow must keep the same link"
    assert token != chat_token(uuid.uuid4())
    assert f"/chat/{flow_id}/{token}" in chat_url(flow_id)


async def test_the_window_describes_itself_without_running_anything(session, organization):
    flow = await published(session, organization)

    drawn = await window(flow.id, chat_token(flow.id), session)

    assert drawn.title == "Support"
    assert drawn.greeting == "What can I help with?"
    # The blank suggestion is dropped rather than rendered as an empty button.
    assert drawn.suggestions == ["Where is my order?", "Refund policy"]

    started = await session.execute(select(Run).where(Run.flow_id == flow.id))
    assert started.scalars().all() == [], "drawing the window must not start a run"


async def test_a_message_starts_a_run_carrying_the_session(session, organization):
    flow = await published(session, organization)

    accepted = await send(
        flow.id,
        chat_token(flow.id),
        ChatMessage(text="Where is my order?", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )

    assert accepted.status is RunStatus.QUEUED
    run = await session.get(Run, accepted.run_id)
    assert run is not None
    # "Who started this?" is the first question asked of a run log, and "a
    # webhook" is the wrong answer when it was a person in a chat window.
    assert run.trigger is TriggerKind.CHAT
    payload = run.input["payload"]
    assert payload["text"] == "Where is my order?"
    # The agent's memory is keyed on this. Without it every message in a
    # conversation arrives as a stranger.
    assert payload["session_id"] == "s-1"


@pytest.mark.parametrize("token", ["", "not-the-token", "0" * 32])
async def test_a_wrong_token_is_the_same_404_as_a_flow_that_does_not_exist(
    session, organization, token
):
    flow = await published(session, organization)

    with pytest.raises(HTTPException) as wrong:
        await window(flow.id, token, session)
    with pytest.raises(HTTPException) as missing:
        await window(uuid.uuid4(), chat_token(uuid.uuid4()), session)

    assert wrong.value.status_code == missing.value.status_code == 404
    assert wrong.value.detail == missing.value.detail


async def test_an_unpublished_flow_has_no_chat_page(session, organization):
    flow, _ = await service.create_flow(
        session,
        organization_id=organization.id,
        user_id=None,
        name="Draft",
        slug=None,
        description=None,
        graph=Graph.model_validate(CHAT_GRAPH),
    )
    await session.commit()

    with pytest.raises(HTTPException) as caught:
        await window(flow.id, chat_token(flow.id), session)
    assert caught.value.status_code == 404


async def test_a_flow_that_does_not_start_with_chat_has_no_chat_page(session, organization):
    """The link is per flow, so a published flow with a schedule trigger would
    otherwise expose a chat box that starts a scheduled job."""
    flow = await published(
        session,
        organization,
        {
            "nodes": [
                {"id": "t", "type": "trigger.manual", "config": {}},
                {
                    "id": "set",
                    "type": "data.set",
                    "config": {"assignments": [{"name": "x", "value": "1"}]},
                },
            ],
            "edges": [{"source": "t", "target": "set"}],
        },
    )

    with pytest.raises(HTTPException) as caught:
        await window(flow.id, chat_token(flow.id), session)
    assert caught.value.status_code == 404


async def test_the_answer_is_the_agents_words_once_the_run_succeeds(session, organization):
    flow = await published(session, organization)
    accepted = await send(
        flow.id,
        chat_token(flow.id),
        ChatMessage(text="hello", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )

    waiting = await answer(
        flow.id, chat_token(flow.id), accepted.run_id, _Request(), Response(), session
    )  # type: ignore[arg-type]
    assert waiting.status is RunStatus.QUEUED
    assert waiting.reply == ""

    run = await session.get(Run, accepted.run_id)
    assert run is not None
    run.status = RunStatus.SUCCEEDED
    run.output = {"result": {"text": "Your order ships tomorrow.", "usage": {}}}
    await session.commit()

    done = await answer(
        flow.id, chat_token(flow.id), accepted.run_id, _Request(), Response(), session
    )  # type: ignore[arg-type]
    assert done.reply == "Your order ships tomorrow."
    assert done.error is None


async def test_a_failed_run_says_so_without_quoting_the_flows_error(session, organization):
    """A flow's error names repositories, models and credentials. It is written
    for the person who built the flow, not for whoever is chatting with it."""
    flow = await published(session, organization)
    accepted = await send(
        flow.id,
        chat_token(flow.id),
        ChatMessage(text="hello", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )
    run = await session.get(Run, accepted.run_id)
    assert run is not None
    run.status = RunStatus.FAILED
    run.error = "Credential 8f2c for github/acme-private was rejected."
    await session.commit()

    replied = await answer(
        flow.id, chat_token(flow.id), accepted.run_id, _Request(), Response(), session
    )  # type: ignore[arg-type]
    assert replied.error and "acme-private" not in replied.error
    assert replied.reply == ""


async def test_a_run_from_another_flow_cannot_be_read_through_this_link(session, organization):
    """The run id is a UUID, but a link holder must not be able to walk one."""
    flow = await published(session, organization)
    other = await published(session, organization)
    accepted = await send(
        other.id,
        chat_token(other.id),
        ChatMessage(text="hello", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )

    with pytest.raises(HTTPException) as caught:
        await answer(flow.id, chat_token(flow.id), accepted.run_id, _Request(), Response(), session)  # type: ignore[arg-type]
    assert caught.value.status_code == 404


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"result": "plain"}, "plain"),
        ({"result": {"text": "from an agent"}}, "from an agent"),
        ({"result": {"reply": "from a node that names it differently"}}, "from a node"),
        ({"result": {"post": {"text": "several terminals"}}}, "several terminals"),
        ({"result": {"artifact_id": "a1"}}, ""),
        (None, ""),
    ],
)
def test_the_reply_is_found_in_the_shapes_a_flow_actually_ends_with(output, expected):
    assert reply_text(output).startswith(expected)


def test_a_chat_page_needs_no_configuration_at_all():
    """Every field has a default that works, because the node is meant to be
    dragged on, connected, published and used."""
    config = ChatTriggerConfig()
    assert config.title and config.greeting and config.placeholder
    assert config.suggestions == []


def test_every_rate_limited_endpoint_takes_the_response_the_limiter_writes_to():
    """SlowAPI writes its headers onto the endpoint's `response` argument and
    raises if there is not one. That raise is a 500, and a 500 skips the CORS
    middleware, so in a browser it arrives as a CORS error about a request that
    was really a crash. The suite runs with rate limiting off, which is exactly
    why this is checked by signature rather than by exercising it."""
    import inspect

    from basivo_orch.flows import chat as module

    for name in ("send", "answer"):
        parameters = inspect.signature(getattr(module, name)).parameters
        assert "response" in parameters, f"{name} is rate limited without a response"
        # `from __future__ import annotations` makes these strings.
        assert parameters["response"].annotation in (Response, "Response")


async def test_a_video_the_run_made_comes_back_as_an_attachment(session, organization):
    """A flow that answers with a file has to be able to show it, and the file
    has to be reachable by someone with the link and nobody else."""
    from basivo_orch.flows.chat import artifact as serve_artifact
    from basivo_orch.flows.models import Artifact

    flow = await published(session, organization)
    accepted = await send(
        flow.id,
        chat_token(flow.id),
        ChatMessage(text="make me a video", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )
    run = await session.get(Run, accepted.run_id)
    assert run is not None

    clip = Artifact(
        organization_id=organization.id,
        run_id=run.id,
        filename="promo.mp4",
        content_type="video/mp4",
        size_bytes=9,
        data=b"not a mp4",
    )
    session.add(clip)
    await session.flush()
    run.status = RunStatus.SUCCEEDED
    run.output = {"result": {"artifact_id": str(clip.id), "url": "/whatever"}}
    await session.commit()

    replied = await answer(
        flow.id, chat_token(flow.id), accepted.run_id, _Request(), Response(), session
    )  # type: ignore[arg-type]
    assert len(replied.attachments) == 1
    attached = replied.attachments[0]
    assert attached.kind == "video"
    assert attached.url.endswith(f"/artifacts/{clip.id}")
    # No words and a file is a complete answer, so nothing apologises for it.
    assert replied.reply == ""

    served = await serve_artifact(
        flow.id, chat_token(flow.id), clip.id, _Request(), Response(), session
    )  # type: ignore[arg-type]
    assert served.body == b"not a mp4"
    assert served.media_type == "video/mp4"
    # Without this the deployment-wide same-origin policy turns every video in
    # a chat window into a broken frame whenever the page and the API are not
    # the same origin.
    assert served.headers["cross-origin-resource-policy"] == "cross-origin"


async def test_a_file_from_another_flows_run_is_not_served_by_this_link(session, organization):
    from basivo_orch.flows.chat import artifact as serve_artifact
    from basivo_orch.flows.models import Artifact

    mine = await published(session, organization)
    theirs = await published(session, organization)
    accepted = await send(
        theirs.id,
        chat_token(theirs.id),
        ChatMessage(text="hello", session_id="s-1"),
        _Request(),  # type: ignore[arg-type]
        Response(),
        session,
    )
    secret = Artifact(
        organization_id=organization.id,
        run_id=accepted.run_id,
        filename="private.png",
        content_type="image/png",
        size_bytes=3,
        data=b"png",
    )
    session.add(secret)
    await session.commit()

    with pytest.raises(HTTPException) as caught:
        await serve_artifact(
            mine.id, chat_token(mine.id), secret.id, _Request(), Response(), session
        )  # type: ignore[arg-type]
    assert caught.value.status_code == 404
