"""The worker: claiming, heartbeating, executing, and recovering.

These run on SQLite, which does not implement `FOR UPDATE SKIP LOCKED` —
SQLAlchemy silently drops the clause. So what is proven here is the *claim
protocol*: a claimed run leaves the queue, a second claimer gets nothing, a
dead worker's run comes back. The row-level locking that makes it safe
under real concurrency is Postgres's own, exercised by the live check rather
than here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import NodeExecution, Run, RunStatus
from basivo_orch.worker import (
    INLINE_GRACE_SECONDS,
    LEASE_SECONDS,
    claim_one,
    execute_claimed,
    heartbeat,
    reap_abandoned,
    worker_identity,
)

SIMPLE_GRAPH = Graph.model_validate(
    {
        "nodes": [
            {"id": "t", "type": "trigger.manual", "config": {}},
            {
                "id": "set",
                "type": "data.set",
                "config": {"assignments": [{"name": "ok", "value": "yes"}]},
            },
        ],
        "edges": [{"source": "t", "target": "set"}],
    }
)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_claiming_takes_a_queued_run_and_marks_it_running(session, make_run):
    run = await make_run(SIMPLE_GRAPH)
    assert run.status is RunStatus.QUEUED

    claimed = await claim_one(session, worker_id="worker-a")

    assert claimed is not None and claimed.id == run.id
    assert claimed.status is RunStatus.RUNNING
    assert claimed.worker_id == "worker-a"
    assert claimed.claimed_at is not None
    # The heartbeat starts at the claim, not at the first beat 15s later —
    # otherwise a run is briefly indistinguishable from an abandoned one.
    assert claimed.heartbeat_at is not None


async def test_a_second_worker_finds_nothing_to_claim(session, make_run):
    await make_run(SIMPLE_GRAPH)

    first = await claim_one(session, worker_id="worker-a")
    second = await claim_one(session, worker_id="worker-b")

    assert first is not None
    assert second is None, "the same run was handed to two workers"


async def test_claiming_an_empty_queue_returns_none(session):
    assert await claim_one(session, worker_id="worker-a") is None


async def test_runs_are_claimed_oldest_first(session, make_run):
    older = await make_run(SIMPLE_GRAPH)
    newer = await make_run(SIMPLE_GRAPH)
    # SQLite's CURRENT_TIMESTAMP has second resolution, so both rows can share
    # a created_at. Make the order unambiguous.
    older.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await session.commit()

    claimed = await claim_one(session, worker_id="worker-a")

    assert claimed is not None and claimed.id == older.id
    assert claimed.id != newer.id


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------


async def test_the_worker_executes_a_claimed_run_to_completion(session, sessions, make_run):
    run = await make_run(SIMPLE_GRAPH)
    claimed = await claim_one(session, worker_id=worker_identity())
    assert claimed is not None

    await execute_claimed(claimed, None, sessions=sessions)

    finished = await session.get(Run, run.id)
    await session.refresh(finished)
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.finished_at is not None

    rows = await session.execute(select(NodeExecution).where(NodeExecution.run_id == run.id))
    assert {row.node_id for row in rows.scalars()} == {"t", "set"}


async def test_a_run_whose_version_vanished_fails_with_a_reason(session, sessions, make_run):
    run = await make_run(SIMPLE_GRAPH)
    claimed = await claim_one(session, worker_id="worker-a")
    assert claimed is not None
    # Simulate the version being gone by pointing the run at nothing real.
    import uuid as _uuid

    claimed.flow_version_id = _uuid.uuid4()
    await session.commit()

    await execute_claimed(claimed, None, sessions=sessions)

    failed = await session.get(Run, run.id)
    await session.refresh(failed)
    assert failed.status is RunStatus.FAILED
    assert "no longer exists" in failed.error


async def test_heartbeat_moves_forward(session, make_run):
    await make_run(SIMPLE_GRAPH)
    claimed = await claim_one(session, worker_id="worker-a")
    assert claimed is not None
    original = claimed.heartbeat_at
    claimed.heartbeat_at = datetime.now(UTC) - timedelta(seconds=60)
    await session.commit()

    await heartbeat(session, claimed.id)

    await session.refresh(claimed)
    # SQLite hands back naive datetimes; compare on the same footing.
    beat = claimed.heartbeat_at.replace(tzinfo=claimed.heartbeat_at.tzinfo or UTC)
    assert beat > (original.replace(tzinfo=original.tzinfo or UTC) - timedelta(seconds=5))


# ---------------------------------------------------------------------------
# Recovery — the reason the lease exists
# ---------------------------------------------------------------------------


async def test_a_run_from_a_dead_worker_goes_back_on_the_queue(session, make_run):
    run = await make_run(SIMPLE_GRAPH)
    claimed = await claim_one(session, worker_id="worker-that-died")
    assert claimed is not None
    # The worker was killed: no heartbeat since well past the lease.
    claimed.heartbeat_at = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS + 30)
    claimed.started_at = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS + 30)
    await session.commit()

    recovered = await reap_abandoned(session)

    assert recovered == [run.id]
    await session.refresh(claimed)
    assert claimed.status is RunStatus.QUEUED
    # Fully released, so the next worker's claim is clean rather than
    # inheriting a stale lease.
    assert claimed.worker_id is None
    assert claimed.claimed_at is None
    assert claimed.heartbeat_at is None

    again = await claim_one(session, worker_id="worker-b")
    assert again is not None and again.id == run.id


async def test_a_live_worker_keeps_its_run(session, make_run):
    await make_run(SIMPLE_GRAPH)
    claimed = await claim_one(session, worker_id="worker-a")
    assert claimed is not None

    assert await reap_abandoned(session) == []

    await session.refresh(claimed)
    assert claimed.status is RunStatus.RUNNING
    assert claimed.worker_id == "worker-a"


async def test_an_inline_run_is_judged_by_the_engine_ceiling_not_the_lease(session, make_run):
    """`mode=sync` runs execute in the API request and never heartbeat.

    Reaping those on the worker lease would steal a run that is still
    executing perfectly well, so they get the engine's own hard ceiling plus
    a margin instead.
    """
    run = await make_run(SIMPLE_GRAPH)
    run.status = RunStatus.RUNNING
    run.worker_id = None
    run.started_at = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS + 60)
    await session.commit()

    assert await reap_abandoned(session) == [], "an inline run was reaped on the worker lease"

    run.started_at = datetime.now(UTC) - timedelta(seconds=INLINE_GRACE_SECONDS + 60)
    await session.commit()

    assert await reap_abandoned(session) == [run.id]


async def test_queued_runs_are_never_reaped(session, make_run):
    """A run waiting for a worker is not a run in trouble."""
    await make_run(SIMPLE_GRAPH)
    assert await reap_abandoned(session) == []


# ---------------------------------------------------------------------------
# Recovery must not repeat work that changed the outside world
# ---------------------------------------------------------------------------


SIDE_EFFECT_GRAPH = Graph.model_validate(
    {
        "nodes": [
            {"id": "t", "type": "trigger.manual", "config": {}},
            {
                "id": "fix",
                "type": "git.autofix",
                "name": "Open the PR",
                "config": {"git_credential_id": "c", "repo": "acme/api", "problem": "x"},
            },
        ],
        "edges": [{"source": "t", "target": "fix"}],
    }
)


async def _abandon(session, run):
    run.status = RunStatus.RUNNING
    run.worker_id = "worker-that-died"
    stale = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS + 30)
    run.heartbeat_at = stale
    run.started_at = stale
    await session.commit()


async def test_a_run_that_already_opened_a_pr_is_not_replayed(session, make_run):
    """Recovery re-runs the graph from the beginning, so replaying a run whose
    autofix node already succeeded would open a SECOND pull request. It is
    failed for a human instead, naming what already happened."""
    from basivo_orch.flows.models import NodeExecution, NodeStatus

    run = await make_run(SIDE_EFFECT_GRAPH)
    await _abandon(session, run)
    session.add(
        NodeExecution(
            run_id=run.id,
            node_id="fix",
            node_type="git.autofix",
            node_name="Open the PR",
            status=NodeStatus.SUCCEEDED,
            attempt=1,
            started_at=datetime.now(UTC),
        )
    )
    await session.commit()

    assert await reap_abandoned(session) == [], "the run was put back on the queue"

    await session.refresh(run)
    assert run.status is RunStatus.FAILED
    assert "Open the PR" in run.error
    assert "repeated" in run.error
    assert await claim_one(session, worker_id="worker-b") is None


async def test_a_run_that_only_read_things_is_replayed_normally(session, make_run):
    """Nothing irreversible happened, so re-running costs nothing but time."""
    from basivo_orch.flows.models import NodeExecution, NodeStatus

    run = await make_run(SIDE_EFFECT_GRAPH)
    await _abandon(session, run)
    session.add(
        NodeExecution(
            run_id=run.id,
            node_id="t",
            node_type="trigger.manual",
            status=NodeStatus.SUCCEEDED,
            attempt=1,
            started_at=datetime.now(UTC),
        )
    )
    await session.commit()

    assert await reap_abandoned(session) == [run.id]
    await session.refresh(run)
    assert run.status is RunStatus.QUEUED


def test_every_node_declares_whether_repeating_it_is_safe():
    """A node that changes the world outside this system must say so, or
    recovery will cheerfully do it twice."""
    from basivo_orch.flows import nodes as registry

    unsafe = {t for t, n in registry.REGISTRY.items() if not n.replay_safe}
    assert unsafe == {"git.ticket", "git.autofix", "git.comment", "http.request"}
