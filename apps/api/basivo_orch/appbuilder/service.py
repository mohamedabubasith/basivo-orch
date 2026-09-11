"""Everything the App Builder does to the database.

Nodes never write SQL, so the turn node asks for what it needs through one
engine-provided callable and this module answers it. Two actions, because a
turn has exactly two moments that touch storage: opening the project, and
finishing with it.

The other half is creating a project, which is where the one structural
decision lives: a project owns a hidden flow with a single node, and a message
is a run of it. That buys the queue, the retries, the live event log and the
Runs screen without a second scheduler, and it is why the builder is a few
hundred lines rather than a subsystem.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.appbuilder import serving
from basivo_orch.appbuilder.models import AppProject, AppTurn, AppVersion, TurnStatus
from basivo_orch.flows.models import Flow, FlowVersion, Run, TriggerKind
from basivo_orch.flows.service import create_run, enqueue

#: How many earlier messages a turn is told about. The agent reads the files
#: for everything else.
HISTORY_TURNS = 6

#: The graph every project gets: a trigger and the node that does the work.
#: It is never edited, so it is written here rather than drawn.
TRIGGER_ID = "start"
BUILD_ID = "build"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:60] or "app"


async def create_project(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    engine: str = "auto",
    credential_id: str = "",
    provider: str = "anthropic",
    model: str = "",
    user_id: uuid.UUID | None = None,
) -> AppProject:
    """A project, and the hidden flow whose runs are its turns."""
    slug = await _free_slug(session, organization_id, slugify(name))

    flow = Flow(
        organization_id=organization_id,
        name=f"App: {name}",
        slug=f"app-{slug}",
        description="Created by the App Builder. Edit the app under Apps, not here.",
        created_by=user_id,
        system=True,
    )
    session.add(flow)
    await session.flush()

    project = AppProject(
        organization_id=organization_id,
        name=name.strip()[:160],
        slug=slug,
        public_slug=await _free_public_slug(session, slug),
        engine=engine,
        flow_id=flow.id,
        created_by=user_id,
    )
    session.add(project)
    await session.flush()

    version = FlowVersion(
        flow_id=flow.id,
        version=1,
        graph={
            "nodes": [
                {"id": TRIGGER_ID, "type": "trigger.manual", "config": {}},
                {
                    "id": BUILD_ID,
                    "type": "app.build",
                    "config": {
                        "project_id": str(project.id),
                        "engine": engine,
                        "credential_id": credential_id,
                        "provider": provider,
                        "model": model,
                    },
                },
            ],
            "edges": [{"source": TRIGGER_ID, "target": BUILD_ID}],
        },
        created_by=user_id,
    )
    session.add(version)
    await session.flush()
    flow.published_version_id = version.id

    await session.commit()
    await session.refresh(project)
    return project


async def _free_slug(session: AsyncSession, organization_id: uuid.UUID, wanted: str) -> str:
    taken = set(
        (
            await session.execute(
                select(AppProject.slug).where(
                    AppProject.organization_id == organization_id,
                    AppProject.slug.like(f"{wanted}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if wanted not in taken:
        return wanted
    for suffix in range(2, 200):
        candidate = f"{wanted}-{suffix}"
        if candidate not in taken:
            return candidate
    return f"{wanted}-{uuid.uuid4().hex[:6]}"


async def _free_public_slug(session: AsyncSession, base: str) -> str:
    """A public address nobody holds. Four random characters make a collision
    a one in a million event; the loop makes it a non event."""
    for _ in range(20):
        candidate = serving.new_public_slug(base)
        taken = await session.execute(
            select(AppProject.id).where(AppProject.public_slug == candidate)
        )
        if taken.scalar_one_or_none() is None:
            return candidate
    return serving.new_public_slug(f"{base}-{uuid.uuid4().hex[:6]}")


async def start_turn(
    session: AsyncSession,
    *,
    project: AppProject,
    message: str,
    user_id: uuid.UUID | None = None,
) -> tuple[AppTurn, Run]:
    """Queue one message. Refuses while another turn is still running.

    One turn at a time, because two agents editing one project would each be
    working from a tree the other is replacing, and the person would get back
    a mixture neither of them can explain.
    """
    running = await session.execute(
        select(AppTurn).where(
            AppTurn.project_id == project.id,
            AppTurn.status.in_([TurnStatus.QUEUED, TurnStatus.RUNNING]),
        )
    )
    if running.scalars().first():
        raise ValueError("This app is still working on the previous message.")

    flow = await session.get(Flow, project.flow_id) if project.flow_id else None
    if flow is None or flow.published_version_id is None:
        raise ValueError("This project is missing its builder. Create it again.")
    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        raise ValueError("This project is missing its builder. Create it again.")

    turn = AppTurn(project_id=project.id, prompt=message.strip()[:8000], created_by=user_id)
    session.add(turn)
    await session.commit()
    await session.refresh(turn)

    run, _ = await create_run(
        session,
        flow=flow,
        version=version,
        trigger=TriggerKind.APP,
        payload={"message": turn.prompt, "turn_id": str(turn.id), "project_id": str(project.id)},
        user_id=user_id,
    )
    turn.run_id = run.id
    await session.commit()
    await session.refresh(turn)
    enqueue(run)
    return turn, run


# ---------------------------------------------------------------------------
# What the node asks for, through the engine
# ---------------------------------------------------------------------------


async def apply(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    action: str,
    project_id: str,
    turn_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """The node's two moments with the database.

    Scoped to the run's workspace, so a project id from another tenant reads
    as missing rather than as a refusal, the same way credentials behave.
    """
    project = await session.get(AppProject, uuid.UUID(project_id))
    if project is None or project.organization_id != organization_id:
        raise ValueError("That app project no longer exists.")
    turn = await session.get(AppTurn, uuid.UUID(turn_id))
    if turn is None or turn.project_id != project.id:
        raise ValueError("That turn no longer exists.")

    if action == "open":
        turn.status = TurnStatus.RUNNING
        await session.commit()
        history = (
            (
                await session.execute(
                    select(AppTurn)
                    .where(
                        AppTurn.project_id == project.id,
                        AppTurn.status == TurnStatus.BUILT,
                        AppTurn.id != turn.id,
                    )
                    .order_by(AppTurn.created_at.desc())
                    .limit(HISTORY_TURNS)
                )
            )
            .scalars()
            .all()
        )
        return {
            "name": project.name,
            "source_artifact_id": str(project.source_artifact_id)
            if project.source_artifact_id
            else "",
            "history": [{"prompt": item.prompt, "reply": item.reply} for item in reversed(history)],
        }

    if action != "finish":
        raise ValueError(f"Unknown app action {action!r}.")

    turn.reply = str(fields.get("reply") or "")[:8000]
    turn.error = str(fields.get("error") or "")[:4000]

    if not fields.get("ok"):
        # The project keeps the tree it had. A build nobody can load is not a
        # version, and storing one would break the preview beside the chat.
        turn.status = TurnStatus.FAILED
        await session.commit()
        return {"version": None}

    highest = await session.execute(
        select(func.max(AppVersion.version)).where(AppVersion.project_id == project.id)
    )
    number = int(highest.scalar() or 0) + 1
    version = AppVersion(
        project_id=project.id,
        version=number,
        source_artifact_id=_as_uuid(fields.get("source_artifact_id")),
        build_artifact_id=_as_uuid(fields.get("build_artifact_id")),
        engine=str(fields.get("engine") or "")[:32],
    )
    session.add(version)
    await session.flush()

    project.source_artifact_id = version.source_artifact_id
    turn.status = TurnStatus.BUILT
    turn.version_id = version.id
    await session.commit()
    return {"version": number}


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Deploying and undoing
# ---------------------------------------------------------------------------


async def publish(session: AsyncSession, *, project: AppProject, version: AppVersion) -> None:
    """Point the share link at a version. This is the whole of Deploy.

    Refreshed afterwards, because `updated_at` is set by the database on
    update: without this the next read of it is a lazy load, and a lazy load
    inside an async request is the MissingGreenlet that turns a successful
    deploy into a 500 the browser reports as a CORS failure.
    """
    project.published_version_id = version.id
    await session.commit()
    await session.refresh(project)


async def unpublish(session: AsyncSession, *, project: AppProject) -> None:
    """Take the public address down. The versions stay; only the pointer goes.

    A person who shared a link and then changed their mind needs this to work
    at once, so it is the same single write as Deploy, in reverse.
    """
    project.published_version_id = None
    await session.commit()
    await session.refresh(project)


async def restore(session: AsyncSession, *, project: AppProject, version: AppVersion) -> None:
    """Make an older version the one the next turn starts from.

    Undo, in one write. The versions after it are left alone: a person who
    goes back two steps and then changes their mind has lost nothing.
    """
    project.source_artifact_id = version.source_artifact_id
    await session.commit()
    await session.refresh(project)
