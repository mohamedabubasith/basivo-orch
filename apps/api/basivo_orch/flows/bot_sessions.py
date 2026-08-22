"""Reading and writing a conversation's state, transactionally.

Separate from the node that uses it because the rules and the row have to move
together. "Add this photo unless it is already there, unless there are already
forty" is a read, a decision and a write; split across a node and the engine it
would be three steps with a gap in the middle, and the gap is where two updates
arriving a millisecond apart both decide they are the first.

So the whole operation happens here, under `SELECT … FOR UPDATE`, and the node
above is a thin shell that names the action. It also keeps to the rule the rest
of the engine follows: nodes do not write SQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows.models import Artifact, BotSession

#: An abandoned job holds someone's wedding photographs. It is swept.
DEFAULT_TTL_HOURS = 48
#: How long a render may hold a chat. Long enough for a slow 30 second render on
#: a small box; short enough that a worker killed mid-render does not leave the
#: bot mute until a human notices.
LOCK_MINUTES = 20
#: Beyond this the montage is a slideshow nobody watches, and the render time
#: stops fitting in a conversation.
MAX_PHOTOS = 40


def _aware(value: datetime | None) -> datetime | None:
    """A stored timestamp, always comparable.

    `DateTime(timezone=True)` is a request, not a guarantee: SQLite has no
    timezone type and hands back a naive value, and some drivers do the same.
    Comparing that to an aware `now()` raises — which would mean the render
    lock throwing instead of holding, on exactly the code path that exists to
    stop two renders at once.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def apply(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    flow_id: uuid.UUID,
    chat_id: str,
    action: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one action against one chat's state and return the result."""
    fields = fields or {}
    now = datetime.now(UTC)

    row = (
        await db.execute(
            select(BotSession)
            .where(BotSession.flow_id == flow_id, BotSession.chat_id == chat_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    is_new = row is None
    if row is None:
        if action == "forget":
            return {**_blank(), "is_new": True}
        row = BotSession(
            organization_id=organization_id,
            flow_id=flow_id,
            chat_id=chat_id,
            photos=[],
            options={},
        )
        db.add(row)
        await db.flush()

    extra = await _mutate(db, row, action, fields, now)
    row.expires_at = now + timedelta(hours=DEFAULT_TTL_HOURS)
    await db.commit()
    await db.refresh(row)
    return {**_render(row, now), **extra, "is_new": is_new}


async def _mutate(
    db: AsyncSession, row: BotSession, action: str, fields: dict[str, Any], now: datetime
) -> dict[str, Any]:
    if action == "read":
        return {}

    if action == "forget":
        # The photographs go too. A "forget" that leaves the pictures in the
        # database is a promise nobody kept — and these are wedding photographs
        # of people who never agreed to anything.
        ids = [uuid.UUID(p["artifact_id"]) for p in row.photos if p.get("artifact_id")]
        if row.last_video_artifact_id:
            ids.append(row.last_video_artifact_id)
        if ids:
            await db.execute(delete(Artifact).where(Artifact.id.in_(ids)))
        row.photos = []
        row.brief = ""
        row.state = "COLLECTING"
        row.iteration = 0
        row.last_video_artifact_id = None
        row.status_message_id = None
        row.locked_until = None
        return {"deleted_files": len(ids)}

    if action == "clear_photos":
        row.photos = []
        return {}

    if action == "add_photo":
        unique = str(fields.get("file_unique_id") or "").strip()
        if unique and any(p.get("file_unique_id") == unique for p in row.photos):
            # The same picture forwarded twice, or a redelivery that slipped
            # past idempotency. Ignoring it is right: nobody asked for it twice.
            return {"duplicate": True, "added": False}
        if len(row.photos) >= MAX_PHOTOS:
            return {"added": False, "rejected": "too_many", "limit": MAX_PHOTOS}
        row.photos = [
            *row.photos,
            {
                "artifact_id": str(fields.get("artifact_id") or ""),
                "file_unique_id": unique,
                "caption": str(fields.get("caption") or "")[:400],
            },
        ]
        return {"added": True}

    if action == "remove_photo":
        target = str(fields.get("file_unique_id") or "")
        before = len(row.photos)
        row.photos = [p for p in row.photos if p.get("file_unique_id") != target]
        return {"removed": before - len(row.photos)}

    if action == "lock":
        held_until = _aware(row.locked_until)
        if held_until and held_until > now:
            # Not an error: the flow branches on this to say "still working on
            # the last one" instead of starting a second render.
            return {"acquired": False}
        row.locked_until = now + timedelta(minutes=LOCK_MINUTES)
        return {"acquired": True}

    if action == "unlock":
        row.locked_until = None
        return {}

    if action == "update":
        # Only the fields present are written. Two nodes editing different
        # parts of one session must not erase each other.
        if state := str(fields.get("state") or "").strip():
            row.state = state
        if brief := str(fields.get("brief") or "").strip():
            row.brief = brief
        if options := fields.get("options"):
            row.options = {**(row.options or {}), **options}
        if (message_id := fields.get("status_message_id")) not in (None, "", 0):
            row.status_message_id = int(message_id)
        if video := str(fields.get("last_video_artifact_id") or "").strip():
            row.last_video_artifact_id = uuid.UUID(video)
        if fields.get("bump_iteration"):
            row.iteration += 1
        if spend := float(fields.get("add_spend_usd") or 0.0):
            row.spend_usd = round((row.spend_usd or 0.0) + spend, 6)
        return {}

    raise ValueError(f"Unknown session action {action!r}.")


def _render(row: BotSession, now: datetime) -> dict[str, Any]:
    return {
        # Carried through so the next node in the chain can address the chat
        # without reaching back past this one.
        "chat_id": row.chat_id,
        "state": row.state,
        "photos": row.photos,
        "photo_count": len(row.photos),
        # The bare list is what a render node wants; the objects carry captions
        # for a flow that needs them.
        "photo_ids": [p.get("artifact_id") for p in row.photos if p.get("artifact_id")],
        "brief": row.brief,
        "options": row.options or {},
        "status_message_id": row.status_message_id,
        "last_video_artifact_id": (
            str(row.last_video_artifact_id) if row.last_video_artifact_id else ""
        ),
        "iteration": row.iteration,
        "spend_usd": row.spend_usd or 0.0,
        "locked": bool((held := _aware(row.locked_until)) and held > now),
    }


def _blank() -> dict[str, Any]:
    return {
        "chat_id": "",
        "state": "COLLECTING",
        "photos": [],
        "photo_count": 0,
        "photo_ids": [],
        "brief": "",
        "options": {},
        "status_message_id": None,
        "last_video_artifact_id": "",
        "iteration": 0,
        "spend_usd": 0.0,
        "locked": False,
    }
