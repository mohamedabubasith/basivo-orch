"""The run event stream.

Section 4 of the SOW asks for something specific and easy to get wrong: a run
started over plain HTTP must be attachable later as a live stream. That rules
out a pure pub/sub design, where anyone who subscribes at t+5s has simply
missed the first five seconds.

So every event is written to `run_event` with a gapless per-run sequence, and
*then* published to Redis. Readers combine the two:

    subscribe first  →  replay from the database  →  flush buffered live events

Subscribing before replaying is what closes the gap. Do it the other way round
and an event emitted between the read and the subscribe is lost by both paths —
a race that shows up as a stream that mysteriously stops one node early, under
load, in production, and never in a test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows.models import RunEvent
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: The Redis client type, as annotated.
#:
#: `types-redis` declares `Redis` generic over the decoded value type, so mypy
#: wants `Redis[str]`. The runtime class is *not* generic, and FastAPI evaluates
#: dependency annotations at import time — so a literal `redis.Redis[str]` in a
#: route signature type-checks cleanly and then crashes the app on startup with
#: "is not a generic class". Aliasing per-context keeps both happy.
if TYPE_CHECKING:
    RedisClient = redis.Redis[str]
else:
    RedisClient = redis.Redis

CHANNEL_PREFIX = "basivo:run:"

#: Emitted last on every run, whatever the outcome. A reader that sees it knows
#: no further events are coming and can close rather than hold the connection
#: open until a timeout.
TERMINAL_EVENTS = frozenset({"run.succeeded", "run.failed", "run.cancelled"})


def channel_for(run_id: uuid.UUID) -> str:
    return f"{CHANNEL_PREFIX}{run_id}"


class EventWriter:
    """Assigns sequence numbers and fans events out. One per run.

    The counter is in memory because exactly one executor owns a run at a time.
    If runs ever move to multiple workers, this becomes a database sequence —
    the unique constraint on (run_id, seq) is there to make that failure loud
    rather than silent.
    """

    def __init__(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        client: RedisClient | None,
        *,
        lock: asyncio.Lock | None = None,
    ):
        self._session = session
        self._run_id = run_id
        self._redis = client
        self._seq = 0
        # Nodes run concurrently and share the engine's session; the engine
        # passes its lock in so an event write cannot interleave with a node
        # row's commit. It also makes `_seq` safe: increment and insert happen
        # together, so sequence numbers stay gapless and in write order.
        self._lock = lock or asyncio.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> RunEvent:
        payload = {
            **(data or {}),
            "timestamp": datetime.now(UTC).isoformat(),
        }

        async with self._lock:
            self._seq += 1
            event = RunEvent(run_id=self._run_id, seq=self._seq, type=event_type, data=payload)
            self._session.add(event)
            # Committed immediately, not batched with the run's other writes. A
            # reader polling the database has to be able to see progress *while*
            # the run is still going; holding events in an open transaction until
            # the run ends would make the live stream arrive all at once at the end.
            await self._session.commit()
            seq = self._seq

        if self._redis is not None:
            message = json.dumps({"seq": seq, "type": event_type, "data": payload})
            try:
                await self._redis.publish(channel_for(self._run_id), message)
            except Exception as exc:
                # A run must not fail because the live fan-out is unavailable.
                # The database copy is authoritative, so readers still converge
                # — they just fall back to polling. Logged because a silent
                # degradation is one nobody notices until a customer asks why
                # the run view stopped updating.
                log.warning("run_event.publish_failed", run_id=str(self._run_id), error=str(exc))

        return event


async def replay(
    session: AsyncSession, run_id: uuid.UUID, *, after: int = 0, limit: int = 1000
) -> list[RunEvent]:
    """Persisted events for a run, in order, after a given sequence."""
    result = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after)
        .order_by(RunEvent.seq)
        .limit(limit)
    )
    return list(result.scalars())


@asynccontextmanager
async def subscription(
    client: RedisClient, run_id: uuid.UUID
) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    """Buffer live events for a run while the caller replays history.

    Yields a queue that starts filling the moment this context is entered, so
    nothing emitted during the replay is lost. Callers drop anything whose
    sequence they already replayed.
    """
    pubsub = client.pubsub()
    await pubsub.subscribe(channel_for(run_id))
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)

    async def pump() -> None:
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    queue.put_nowait(json.loads(message["data"]))
                except asyncio.QueueFull:
                    # A reader too slow to keep up is dropped rather than
                    # allowed to grow the buffer without bound. It can
                    # reconnect with Last-Event-ID and catch up from the
                    # database, which is exactly what that header is for.
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("run_event.subscription_dropped", run_id=str(run_id), error=str(exc))

    task = asyncio.create_task(pump())
    try:
        yield queue
    finally:
        task.cancel()
        # Teardown is best-effort: the connection may already be gone because
        # the client disconnected, which is the normal way a stream ends.
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel_for(run_id))
        with contextlib.suppress(Exception):
            await pubsub.aclose()  # type: ignore[attr-defined]


def sse_frame(seq: int, event_type: str, data: dict[str, Any]) -> str:
    """One Server-Sent Event.

    `id:` carries the sequence, which is what the browser echoes back as
    `Last-Event-ID` when EventSource reconnects — the reconnect path is
    automatic in every browser, so getting this field right is what makes a
    dropped connection recoverable rather than a silently truncated run.
    """
    body = json.dumps(data, default=str)
    return f"id: {seq}\nevent: {event_type}\ndata: {body}\n\n"
