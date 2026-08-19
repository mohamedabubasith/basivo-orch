"""Files a run produced: stored, scoped, and served back.

The scoping test is the important one. A poster can carry unreleased copy, and
an id in a URL is not a permission — a workspace must not be able to read
another's files by guessing.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.flows.engine import Engine
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import Artifact

TRIVIAL = Graph.model_validate(
    {"nodes": [{"id": "t", "type": "trigger.manual", "config": {}}], "edges": []}
)


async def test_saving_returns_a_reference_and_stores_the_bytes(session, make_run):
    run = await make_run(TRIVIAL)
    engine = Engine(session, run=run, graph=TRIVIAL, redis_client=None)

    saved = await engine._save_artifact(
        b"\x89PNG\r\n\x1a\nhello", filename="poster.png", content_type="image/png", node_id="p"
    )

    assert saved["size_bytes"] == 13
    assert saved["content_type"] == "image/png"
    assert saved["url"].endswith(saved["artifact_id"])

    stored = await session.get(Artifact, uuid.UUID(saved["artifact_id"]))
    assert stored.data == b"\x89PNG\r\n\x1a\nhello"
    assert stored.run_id == run.id
    # Loading it back is what the posting node does.
    assert await engine._load_artifact(saved["artifact_id"]) == stored.data


async def test_a_file_from_another_workspace_is_not_readable(session, make_run):
    run = await make_run(TRIVIAL)
    engine = Engine(session, run=run, graph=TRIVIAL, redis_client=None)

    stranger = Artifact(
        organization_id=uuid.uuid4(),  # a different workspace
        filename="secret.png",
        content_type="image/png",
        size_bytes=3,
        data=b"abc",
    )
    session.add(stranger)
    await session.commit()

    assert await engine._load_artifact(str(stranger.id)) is None
    assert await engine._load_artifact("not-a-uuid") is None
    assert await engine._load_artifact(str(uuid.uuid4())) is None


async def test_an_oversized_file_is_refused_with_the_limit_named(session, make_run):
    from basivo_orch.flows.nodes.base import NodeError

    run = await make_run(TRIVIAL)
    engine = Engine(session, run=run, graph=TRIVIAL, redis_client=None)

    with pytest.raises(NodeError, match="limit"):
        await engine._save_artifact(b"x" * (Engine.MAX_ARTIFACT_BYTES + 1), filename="huge.png")


async def test_the_endpoint_refuses_another_workspaces_file(session):
    """The route, not just the engine: an id in a URL is not a permission."""
    from basivo_orch.flows.router import read_artifact

    stranger = Artifact(
        organization_id=uuid.uuid4(),
        filename="secret.png",
        content_type="image/png",
        size_bytes=3,
        data=b"abc",
    )
    session.add(stranger)
    await session.commit()

    class _Context:
        organization_id = uuid.uuid4()

    with pytest.raises(HTTPException) as excinfo:
        await read_artifact(stranger.id, context=_Context(), session=session)
    assert excinfo.value.status_code == 404
