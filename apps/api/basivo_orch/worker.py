"""The run worker. `python -m basivo_orch.worker`.

Runs used to execute inside the API process as `asyncio.create_task`, which
meant a deploy, a reload, or an OOM silently killed every run in flight — a
four-minute agent run is a long time to be one `uvicorn --reload` away from
vanishing. Execution belongs in a process whose lifecycle is its own.

There is no new infrastructure here, because the queue already existed: every
run is written to Postgres as QUEUED before anything executes it. That table
*is* the job queue, claimed with `FOR UPDATE SKIP LOCKED` — the standard
Postgres queue pattern. Two workers cannot take the same run, and a worker
that dies holding one is found by its stale heartbeat and the run is handed
back rather than left RUNNING forever.

What this process owns:

* claiming and executing queued runs (`claim_one` / `execute_claimed`)
* the heartbeat that proves it is still alive (`heartbeat`)
* the reaper that recovers runs from dead workers (`reap_abandoned`)
* the schedule ticker, which belongs here for the same reason: a cron that
  only fires while someone is serving HTTP is not a cron.

Scaling is `docker compose up --scale worker=3`. The claim is already
multi-worker-safe; nothing else has to change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.config import get_settings
from basivo_orch.db import SessionLocal, dispose_engine
from basivo_orch.flows.engine import RUN_TIMEOUT_SECONDS, Engine
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.graph import Graph
from basivo_orch.flows.models import FlowVersion, Run, RunStatus
from basivo_orch.flows.scheduler import run_scheduler
from basivo_orch.logging import configure_logging, get_logger

# Registers EVERY table on the shared metadata, not just the flow ones.
# Without it, `run.organization_id`'s foreign key has no `organization` table
# to point at and every claim dies at mapper configuration — the worker stays
# up, retries once a second, and executes nothing. The API only gets away
# with importing less because its routers pull the auth models in anyway.
import basivo_orch.models  # noqa: F401  isort:skip

log = get_logger(__name__)

#: How often an idle worker asks for work. The query is an index lookup on
#: (status, created_at); a second of latency on a webhook-triggered run is
#: invisible next to the run itself.
POLL_SECONDS = 1.0

#: How often a running worker proves it is alive.
HEARTBEAT_SECONDS = 15

#: A claimed run whose heartbeat is older than this is considered abandoned.
#: Comfortably more than several missed heartbeats, so a worker paused by a
#: slow query or a GC hiccup is never robbed of a run it is still executing.
LEASE_SECONDS = 90

#: Runs executed inline by an API request (`mode=sync`) never heartbeat, so
#: they are judged by the engine's own hard ceiling instead. Past this, the
#: process that was executing it is definitively gone.
INLINE_GRACE_SECONDS = RUN_TIMEOUT_SECONDS + 300

#: How many runs one worker executes at once.
#: How many runs one worker executes at once. Two, not four: runs are mostly
#: waiting on models, but the heavy ones (render, speech) are gated to one per
#: process anyway, and a smaller number keeps peak memory predictable — which
#: on a box shared with Postgres is worth more than theoretical throughput.
#: Scale out with more worker containers rather than up with this.
MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("BASIVO_MAX_CONCURRENT_RUNS", "2")))

#: Restart this process when it grows past this, once it is idle. Not a fix for
#: a leak — a bound on one. Long-lived processes that load ONNX sessions, spawn
#: browsers and hold agent conversations accumulate memory that no single line
#: of code is responsible for, and a worker that quietly reaches the container
#: limit is OOM-killed mid-run. Exiting cleanly between runs costs a few
#: seconds of startup and gives the memory back.
#:
#: Zero disables it.
RSS_LIMIT_MB = int(os.environ.get("BASIVO_WORKER_RSS_LIMIT_MB", "0"))

#: Housekeeping cadence: idle-model unload, artifact retention, temp sweep.
HOUSEKEEPING_SECONDS = float(os.environ.get("BASIVO_HOUSEKEEPING_SECONDS", "300"))

#: Delete rendered artifacts older than this. They are reproducible outputs,
#: not records: a poster from March is 500KB in the database, in every backup,
#: and of interest to nobody. Zero keeps them forever.
ARTIFACT_RETENTION_DAYS = int(os.environ.get("BASIVO_ARTIFACT_RETENTION_DAYS", "30"))


def worker_identity() -> str:
    """Host and pid — enough to find the process that holds a run."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def claim_one(session: AsyncSession, *, worker_id: str) -> Run | None:
    """Take the oldest queued run, or return None.

    `SKIP LOCKED` is what makes this safe with several workers: a row another
    worker has locked is passed over rather than waited on, so workers never
    queue up behind each other on the same job. The status flip to RUNNING is
    committed as part of the claim — the run is spoken for before a single
    node executes.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(Run)
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    run = result.scalar_one_or_none()
    if run is None:
        await session.rollback()
        return None

    run.status = RunStatus.RUNNING
    run.worker_id = worker_id
    run.claimed_at = now
    run.heartbeat_at = now
    await session.commit()
    return run


async def heartbeat(session: AsyncSession, run_id: uuid.UUID) -> None:
    await session.execute(
        update(Run).where(Run.id == run_id).values(heartbeat_at=datetime.now(UTC))
    )
    await session.commit()


async def execute_claimed(
    run: Run,
    redis_client: RedisClient | None,
    *,
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
) -> None:
    """Execute a claimed run, heartbeating until it finishes.

    The heartbeat gets its own session: the engine's session is busy inside
    node transactions for minutes at a time, and a heartbeat that has to wait
    for a model call to finish is not a heartbeat.

    `sessions` is injectable so tests can bind this to their own database.
    Production passes nothing and gets the app's sessionmaker.
    """

    async def beat() -> None:
        async with sessions() as beat_session:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await heartbeat(beat_session, run.id)

    beater = asyncio.create_task(beat())
    try:
        async with sessions() as session:
            fresh = await session.get(Run, run.id)
            if fresh is None:
                return
            version = await session.get(FlowVersion, fresh.flow_version_id)
            if version is None:
                fresh.status = RunStatus.FAILED
                fresh.error = "The flow version this run belongs to no longer exists."
                await session.commit()
                return
            graph = Graph.model_validate(version.graph)
            await Engine(session, run=fresh, graph=graph, redis_client=redis_client).execute()
    except Exception as exc:  # noqa: BLE001 — a crash must not take the worker down
        log.exception("worker.run_crashed", run_id=str(run.id), error=str(exc))
        async with sessions() as session:
            crashed = await session.get(Run, run.id)
            if crashed is not None and not crashed.status.is_terminal:
                crashed.status = RunStatus.FAILED
                crashed.error = f"The worker crashed while executing this run: {exc}"[:2000]
                crashed.finished_at = datetime.now(UTC)
                await session.commit()
    finally:
        beater.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beater


async def reap_abandoned(session: AsyncSession, *, now: datetime | None = None) -> list[uuid.UUID]:
    """Hand back runs whose executor died. Returns the run ids recovered.

    A worker-claimed run is judged by its heartbeat; a run executed inline by
    an API request never had one, so it is judged by the engine's hard
    timeout plus a wide margin. Recovered runs go back to QUEUED rather than
    straight to FAILED: the common cause is a deploy, and re-running is what
    the user wanted to happen in the first place.
    """
    now = now or datetime.now(UTC)
    result = await session.execute(
        select(Run).where(
            Run.status == RunStatus.RUNNING,
            (
                (Run.worker_id.is_not(None))
                & (Run.heartbeat_at < now - timedelta(seconds=LEASE_SECONDS))
            )
            | (
                (Run.worker_id.is_(None))
                & (Run.started_at < now - timedelta(seconds=INLINE_GRACE_SECONDS))
            ),
        )
    )
    recovered: list[uuid.UUID] = []
    touched = False
    for run in result.scalars():
        log.warning(
            "worker.run_abandoned",
            run_id=str(run.id),
            worker_id=run.worker_id,
            last_heartbeat=run.heartbeat_at.isoformat() if run.heartbeat_at else None,
        )

        if repeated := await irreversible_steps_taken(session, run.id):
            # Recovery re-runs the graph from the beginning, so a run that has
            # already opened a pull request would open a second one. Nobody
            # thanks an automation for that. It is failed for a human instead,
            # naming exactly what already happened.
            run.status = RunStatus.FAILED
            run.error = (
                "The worker executing this run stopped before it finished. It was not "
                "restarted automatically because these steps had already completed and "
                f"would have been repeated: {', '.join(repeated)}. Check what they left "
                "behind, then run it again if it is still needed."
            )
            run.finished_at = now
            log.warning("worker.run_not_replayed", run_id=str(run.id), completed=repeated)
            touched = True
            continue

        run.status = RunStatus.QUEUED
        run.worker_id = None
        run.claimed_at = None
        run.heartbeat_at = None
        run.started_at = None
        recovered.append(run.id)
        touched = True
    # Committed on any change, not just recoveries: a run failed as
    # unrepeatable is a change too, and guarding the commit on `recovered`
    # alone left it sitting in RUNNING forever.
    if touched:
        await session.commit()
    return recovered


async def irreversible_steps_taken(session: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Names of already-succeeded nodes in this run that must not run twice.

    Read from the node records the engine writes as it goes, which is why they
    are written as each node finishes rather than batched at the end.
    """
    from basivo_orch.flows import nodes as registry
    from basivo_orch.flows.models import NodeExecution, NodeStatus

    result = await session.execute(
        select(NodeExecution).where(
            NodeExecution.run_id == run_id, NodeExecution.status == NodeStatus.SUCCEEDED
        )
    )
    unsafe = []
    for record in result.scalars():
        implementation = registry.REGISTRY.get(record.node_type)
        if implementation is not None and not implementation.replay_safe:
            unsafe.append(record.node_name or record.node_id)
    return unsafe


async def work_loop(redis_client: RedisClient | None, stopping: asyncio.Event) -> None:
    """Claim and execute until asked to stop."""
    worker_id = worker_identity()
    running: set[asyncio.Task[None]] = set()
    log.info("worker.started", worker_id=worker_id, max_concurrent=MAX_CONCURRENT_RUNS)

    while not stopping.is_set():
        if len(running) >= MAX_CONCURRENT_RUNS:
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            continue

        claimed: Run | None = None
        try:
            async with SessionLocal() as session:
                claimed = await claim_one(session, worker_id=worker_id)
        except Exception as exc:  # noqa: BLE001 — the database may be restarting
            log.warning("worker.claim_failed", error=str(exc))

        if claimed is None:
            if should_recycle(in_flight=len(running)):
                # Idle and over the limit: the cleanest moment there is.
                log.warning(
                    "worker.recycling",
                    resident_mb=round(resident_mb()),
                    limit_mb=RSS_LIMIT_MB,
                    note="restarting to return memory; queued runs are untouched",
                )
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=POLL_SECONDS)
            continue

        log.info("worker.run_claimed", run_id=str(claimed.id), worker_id=worker_id)
        task = asyncio.create_task(execute_claimed(claimed, redis_client))
        running.add(task)
        task.add_done_callback(running.discard)

    if running:
        # Finish what we started. A worker that drops four in-flight runs on
        # SIGTERM has just recreated the problem it exists to solve.
        log.info("worker.draining", in_flight=len(running))
        await asyncio.gather(*running, return_exceptions=True)


def resident_mb() -> float:
    """This process's resident memory, in MB.

    Read from /proc where it exists and from `resource` otherwise, because the
    same code runs in a Linux container and on a developer's Mac — and on Linux
    `ru_maxrss` is a high-water mark, which would make a worker restart itself
    forever after one expensive run.
    """
    try:
        with open("/proc/self/statm") as handle:  # noqa: PTH123 — a proc file, not a path
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
    except (OSError, IndexError, ValueError):
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        return usage / 1024 if sys.platform != "darwin" else usage / 1_048_576


def should_recycle(*, in_flight: int, limit_mb: int = RSS_LIMIT_MB) -> bool:
    """Whether to hand this process back to the supervisor.

    Only ever between runs. Exiting with work in flight would abandon runs the
    reaper then has to reclaim — trading a memory problem for a correctness
    one.
    """
    if limit_mb <= 0 or in_flight:
        return False
    return resident_mb() > limit_mb


async def sweep_artifacts(session: AsyncSession, *, days: int = ARTIFACT_RETENTION_DAYS) -> int:
    """Delete rendered files past their retention. Returns how many.

    Deleted in batches rather than one statement: a single DELETE covering
    months of 1MB rows takes a long lock and writes an enormous WAL segment,
    and this runs beside a live API on the same small box.
    """
    if days <= 0:
        return 0

    from basivo_orch.flows.models import Artifact

    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    for _ in range(20):  # a bounded amount of work per housekeeping tick
        result = await session.execute(
            select(Artifact.id).where(Artifact.created_at < cutoff).limit(50)
        )
        ids = [row for row in result.scalars()]
        if not ids:
            break
        await session.execute(delete(Artifact).where(Artifact.id.in_(ids)))
        await session.commit()
        removed += len(ids)
    if removed:
        log.info("worker.artifacts_swept", removed=removed, older_than_days=days)
    return removed


def sweep_temp_directories(prefix: str = "basivo-", older_than_seconds: float = 3600) -> int:
    """Remove render scratch directories a killed process left behind.

    `TemporaryDirectory` cleans up on exit; it cannot clean up after SIGKILL,
    and a render's frame directory is hundreds of megabytes. On a 50GB disk
    shared with the database, a handful of those is the difference between a
    slow week and a full disk — and a full disk under Postgres is not a slow
    week.
    """
    import shutil
    import tempfile
    import time as _time

    root = pathlib.Path(tempfile.gettempdir())
    now = _time.time()
    removed = 0
    for entry in root.glob(f"{prefix}*"):
        try:
            if not entry.is_dir() or now - entry.stat().st_mtime < older_than_seconds:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("worker.temp_swept", removed=removed)
    return removed


async def housekeeping_loop(stopping: asyncio.Event) -> None:
    """The chores that keep a long-lived worker from growing without bound."""
    from basivo_orch.flows.nodes.speech import release_engine_if_idle

    while not stopping.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=HOUSEKEEPING_SECONDS)
        if stopping.is_set():
            return
        try:
            await asyncio.to_thread(release_engine_if_idle)
            await asyncio.to_thread(sweep_temp_directories)
            async with SessionLocal() as session:
                await sweep_artifacts(session)
        except Exception as exc:  # noqa: BLE001 — chores must never kill the worker
            log.warning("worker.housekeeping_failed", error=str(exc))


async def reaper_loop(stopping: asyncio.Event) -> None:
    while not stopping.is_set():
        try:
            async with SessionLocal() as session:
                await reap_abandoned(session)
        except Exception as exc:  # noqa: BLE001
            log.warning("worker.reap_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=LEASE_SECONDS / 3)


async def main() -> None:
    settings = get_settings()
    configure_logging(json_logs=settings.is_production)

    client: RedisClient | None = None
    try:
        client = redis.from_url(get_auth_settings().redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — Redis is for live streaming only
        log.warning(
            "redis.unavailable", error=str(exc), impact="run streaming falls back to polling"
        )
        client = None

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    tasks = [
        asyncio.create_task(work_loop(client, stopping)),
        asyncio.create_task(reaper_loop(stopping)),
        asyncio.create_task(housekeeping_loop(stopping)),
        asyncio.create_task(run_scheduler(client)),
    ]
    try:
        await tasks[0]  # the work loop drains on stop; the others are cancelled
    finally:
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        if client is not None:
            await client.aclose()  # type: ignore[attr-defined]
        await dispose_engine()
        log.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
