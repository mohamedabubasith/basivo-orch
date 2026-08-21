"""Resource discipline: the stack must fit in half the box and stay there.

These are the guarantees that keep one machine running Postgres, Redis, the
API and two workers without the kernel choosing a victim. Each test exists
because the failure it prevents is invisible until production: memory that
grows across a week of runs, four browsers on two cores, a full disk under the
database, a worker OOM-killed mid-render.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from basivo_orch.flows import nodes as registry
from basivo_orch.flows.engine import HEAVY_CONCURRENCY, heavy_slot
from basivo_orch.flows.models import Artifact
from basivo_orch.worker import (
    MAX_CONCURRENT_RUNS,
    resident_mb,
    should_recycle,
    sweep_artifacts,
    sweep_temp_directories,
)

# ---------------------------------------------------------------------------
# The heavy gate
# ---------------------------------------------------------------------------


def test_the_cpu_bound_nodes_are_the_ones_marked_heavy():
    """Marked by hand, so this pins the list. An unmarked render node would
    run four-up on a two-core box and nothing would say why it got slower."""
    heavy = {node_type for node_type, cls in registry.REGISTRY.items() if cls.heavy}
    assert heavy == {"video.render", "video.generate", "design.render", "audio.speak"}

    # And the ones that are merely *slow* stay unmarked: an agent call is
    # minutes of waiting on someone else's GPU, and serialising those would
    # make a flow with three agents three times slower for no reason.
    assert registry.REGISTRY["agent.llm"].heavy is False
    assert registry.REGISTRY["http.request"].heavy is False


async def test_only_one_heavy_node_runs_at_a_time():
    """The whole point of the gate."""
    assert HEAVY_CONCURRENCY == 1, "the default has to be one, whatever the env says"

    gate = heavy_slot()
    overlapping = 0
    peak = 0

    async def heavy_work():
        nonlocal overlapping, peak
        async with gate:
            overlapping += 1
            peak = max(peak, overlapping)
            await asyncio.sleep(0.02)
            overlapping -= 1

    await asyncio.gather(*(heavy_work() for _ in range(4)))
    assert peak == 1, "two renders overlapped"


async def test_the_gate_is_released_when_a_heavy_node_fails():
    """A render that raises must not take the slot with it — the next run would
    block until the process restarted."""
    gate = heavy_slot()
    with pytest.raises(RuntimeError):
        async with gate:
            raise RuntimeError("render blew up")
    assert not gate.locked()


# ---------------------------------------------------------------------------
# Worker memory
# ---------------------------------------------------------------------------


def test_two_runs_at_once_not_four():
    """Peak memory is more valuable than theoretical throughput on a shared
    box; capacity comes from more containers, not a bigger number here."""
    assert MAX_CONCURRENT_RUNS == 2


def test_resident_memory_is_a_current_reading_not_a_high_water_mark():
    """On Linux `ru_maxrss` is a peak, and a worker that judged itself by its
    peak would restart forever after one expensive render."""
    before = resident_mb()
    ballast = bytearray(60 * 1_048_576)
    after = resident_mb()
    del ballast
    assert before > 0
    assert after >= before, "allocating 60MB should not lower the reading"


def test_a_worker_never_recycles_with_work_in_flight():
    """Exiting mid-run trades a memory problem for abandoned runs the reaper
    then has to reclaim."""
    assert should_recycle(in_flight=1, limit_mb=1) is False
    assert should_recycle(in_flight=3, limit_mb=1) is False
    # Idle and over the limit is the one case that returns True.
    assert should_recycle(in_flight=0, limit_mb=1) is True


def test_recycling_is_off_unless_a_limit_is_set():
    """A development machine should not have processes exiting under it."""
    assert should_recycle(in_flight=0, limit_mb=0) is False
    assert should_recycle(in_flight=0, limit_mb=-1) is False


async def test_the_voice_model_is_dropped_when_idle():
    """400MB held for a workspace that speaks twice a day is how a long-lived
    worker's memory doubles and stays there."""
    from basivo_orch.flows.nodes import speech

    class _Session:
        def get_outputs(self):
            return []

    class _Engine:
        sess = _Session()
        has_timings = True

    speech._engine = _Engine()
    speech._engine_last_used = 0.0

    # Recently used: kept.
    speech._engine_last_used = 10_000_000.0
    assert speech.release_engine_if_idle(now=10_000_001.0) is False
    assert speech._engine is not None

    # Idle past the threshold: dropped.
    assert speech.release_engine_if_idle(now=10_000_001.0 + speech.IDLE_UNLOAD_SECONDS) is True
    assert speech._engine is None
    # Idempotent — housekeeping runs every five minutes.
    assert speech.release_engine_if_idle() is False


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


def test_a_render_refuses_to_start_without_scratch_space():
    """Filling the disk does not fail a render, it stops Postgres accepting
    writes. Failing one node is the cheaper outcome by a wide margin."""
    from basivo_orch.flows.nodes.base import NodeError
    from basivo_orch.flows.nodes.video import ensure_disk_space, free_disk_gb

    assert free_disk_gb() > 0
    ensure_disk_space(0.001)  # plenty free: no complaint

    with pytest.raises(NodeError, match="disk is free") as raised:
        ensure_disk_space(1_000_000)
    # Not retryable: waiting and trying again will not create disk space.
    assert raised.value.retryable is False


def test_orphaned_render_directories_are_swept(tmp_path, monkeypatch):
    """`TemporaryDirectory` cannot clean up after SIGKILL, and a render's frame
    directory is hundreds of megabytes."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    stale = tmp_path / "basivo-video-old"
    stale.mkdir()
    (stale / "frame.png").write_bytes(b"x" * 1024)
    import os

    os.utime(stale, (0, 0))  # long abandoned

    fresh = tmp_path / "basivo-video-running"
    fresh.mkdir()
    unrelated = tmp_path / "somebody-elses-dir"
    unrelated.mkdir()
    os.utime(unrelated, (0, 0))

    assert sweep_temp_directories() == 1
    assert not stale.exists()
    assert fresh.exists(), "a render in progress must survive"
    assert unrelated.exists(), "only our own prefix"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


async def test_artifacts_past_retention_are_deleted(session, organization):
    """A poster from March is 500KB in the database, in every backup, and of
    interest to nobody."""
    old = Artifact(
        organization_id=organization.id,
        run_id=None,
        node_id="poster",
        filename="old.png",
        content_type="image/png",
        data=b"x" * 32,
        size_bytes=32,
        created_at=datetime.now(UTC) - timedelta(days=90),
    )
    recent = Artifact(
        organization_id=organization.id,
        run_id=None,
        node_id="poster",
        filename="new.png",
        content_type="image/png",
        data=b"x" * 32,
        size_bytes=32,
    )
    session.add_all([old, recent])
    await session.commit()

    removed = await sweep_artifacts(session, days=30)
    assert removed == 1

    surviving = (await session.execute(__import__("sqlalchemy").select(Artifact))).scalars().all()
    assert [a.filename for a in surviving] == ["new.png"]

    # Retention off keeps everything.
    assert await sweep_artifacts(session, days=0) == 0


# ---------------------------------------------------------------------------
# Bandwidth and memory on the HTTP node
# ---------------------------------------------------------------------------


async def test_an_oversized_response_is_stopped_not_downloaded_then_rejected():
    """The limit used to run *after* `response.content`.

    That meant a 2GB reply was fully downloaded and fully in memory before
    being refused: a bandwidth bill and an OOM-killed worker, in exchange for
    an error message. This counts the bytes the server was actually asked for.
    """
    import httpx

    from basivo_orch.flows.nodes.base import NodeError
    from basivo_orch.flows.nodes.http import MAX_RESPONSE_BYTES, HttpRequestConfig, HttpRequestNode

    sent = {"bytes": 0}
    chunk = b"x" * 65536

    async def endless(request: httpx.Request) -> httpx.Response:
        async def stream():
            # Twenty times the cap, offered a chunk at a time — an async
            # generator, because an async client streams through
            # AsyncByteStream and a sync one is silently read whole.
            for _ in range((MAX_RESPONSE_BYTES * 20) // len(chunk)):
                sent["bytes"] += len(chunk)
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/plain"}, content=stream())

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endless)) as client:
        with pytest.raises(NodeError, match="was stopped"):
            await HttpRequestNode().run(
                HttpRequestConfig(url="https://example.com/huge"),
                _context(recorder, http=client),
            )

    # The transfer stopped near the cap rather than running to 100MB.
    assert sent["bytes"] <= MAX_RESPONSE_BYTES + 2 * len(chunk), (
        f"downloaded {sent['bytes']} bytes for a {MAX_RESPONSE_BYTES}-byte limit"
    )


async def test_a_declared_oversize_length_costs_no_bytes_at_all():
    """A server that announces 900MB is refused before the body is read."""
    import httpx

    from basivo_orch.flows.nodes.base import NodeError
    from basivo_orch.flows.nodes.http import HttpRequestConfig, HttpRequestNode

    async def honest(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream", "content-length": "943718400"},
            content=b"",
        )

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(honest)) as client:
        with pytest.raises(NodeError, match="Nothing was downloaded"):
            await HttpRequestNode().run(
                HttpRequestConfig(url="https://example.com/big.iso"),
                _context(recorder, http=client),
            )


async def test_a_normal_response_still_parses():
    """The streaming rewrite must not change what a well-behaved reply does."""
    import httpx

    from basivo_orch.flows.nodes.http import HttpRequestConfig, HttpRequestNode

    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "green", "count": 3})

    recorder = _Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        result = await HttpRequestNode().run(
            HttpRequestConfig(url="https://example.com/api"),
            _context(recorder, http=client),
        )
    assert result.output["body"] == {"status": "green", "count": 3}
    assert result.output["status"] == 200


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass


def _context(recorder: _Recorder, *, http):
    from basivo_orch.flows.nodes.base import NodeContext

    async def resolve_credential(_id: str):
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="http_1",
        node_name="Fetch",
        attempt=1,
        input={},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
    )
