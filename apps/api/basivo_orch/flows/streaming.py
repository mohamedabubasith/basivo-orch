"""The SSE reader.

Section 4's cross-mode requirement lands here: this generator works whether the
run is queued, halfway through, or finished before the caller arrived. It does
that by treating the database as the record and Redis as the live tail, and by
subscribing *before* it replays so nothing falls between the two.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.db import SessionLocal
from basivo_orch.flows.events import TERMINAL_EVENTS, RedisClient, replay, sse_frame, subscription
from basivo_orch.flows.models import Run

#: Sent when nothing has happened for a while. Proxies and load balancers close
#: idle connections, and a flow whose first node is a slow HTTP call can easily
#: be quiet for a minute.
HEARTBEAT_SECONDS = 15

#: Ceiling on how long a client may hold a stream open.
MAX_STREAM_SECONDS = 3600


async def event_stream(
    run_id: uuid.UUID,
    *,
    organization_id: uuid.UUID,
    redis_client: RedisClient | None,
    after: int = 0,
) -> AsyncIterator[str]:
    """Yield SSE frames for a run until it reaches a terminal state.

    `after` is the last sequence the client already has — from `Last-Event-ID`
    on an EventSource reconnect, so a dropped connection resumes rather than
    replaying from the beginning or, worse, silently skipping the gap.
    """
    # Its own session, held for the life of the stream. The request's session
    # is released when the response starts, and a streaming response outlives
    # that by design.
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None or run.organization_id != organization_id:
            yield sse_frame(0, "error", {"error": "No such run."})
            return

        if redis_client is None:
            # No live tail available. Polling is slower but correct, and it is
            # the same code path a deployment without Redis would take.
            async for frame in _poll_only(session, run_id, after=after):
                yield frame
            return

        async with subscription(redis_client, run_id) as live:
            highest = after

            # History first. The subscription above is already buffering, so
            # anything emitted during this read is waiting in `live`.
            for event in await replay(session, run_id, after=after):
                highest = max(highest, event.seq)
                yield sse_frame(event.seq, event.type, event.data)
                if event.type in TERMINAL_EVENTS:
                    return

            # A run that finished before we subscribed has no more events
            # coming; without this check the loop below would sit here until
            # the stream timeout.
            await session.refresh(run)
            if run.status.is_terminal:
                return

            deadline = asyncio.get_running_loop().time() + MAX_STREAM_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                try:
                    message = await asyncio.wait_for(live.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    # A comment frame: valid SSE, ignored by EventSource, and
                    # enough to keep an idle connection from being reaped.
                    yield ": keep-alive\n\n"

                    # Belt and braces. If the publish was lost — Redis
                    # restarted, the network blipped — the database still knows
                    # the run ended, and the client gets closure either way.
                    await session.refresh(run)
                    if run.status.is_terminal:
                        for event in await replay(session, run_id, after=highest):
                            yield sse_frame(event.seq, event.type, event.data)
                        return
                    continue

                seq = message.get("seq", 0)
                if seq <= highest:
                    continue  # already delivered during replay
                highest = seq
                yield sse_frame(seq, message["type"], message.get("data", {}))
                if message["type"] in TERMINAL_EVENTS:
                    return

            yield sse_frame(highest, "stream.timeout", {"reason": "Stream held open too long."})


async def _poll_only(session: AsyncSession, run_id: uuid.UUID, *, after: int) -> AsyncIterator[str]:
    """Fallback when there is no Redis: read new events from the database."""
    highest = after
    deadline = asyncio.get_running_loop().time() + MAX_STREAM_SECONDS

    while asyncio.get_running_loop().time() < deadline:
        events = await replay(session, run_id, after=highest)
        for event in events:
            highest = event.seq
            yield sse_frame(event.seq, event.type, event.data)
            if event.type in TERMINAL_EVENTS:
                return
        if not events:
            yield ": keep-alive\n\n"
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.5)

    yield sse_frame(highest, "stream.timeout", {"reason": "Stream held open too long."})


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx buffers proxied responses by default, which holds every event until
    # the run ends and then delivers them at once — turning a live stream into
    # a slow request. This is the documented opt-out.
    "X-Accel-Buffering": "no",
}
