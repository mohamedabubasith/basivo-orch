"""The App Builder over HTTP: projects, messages, versions, and the site itself.

Two halves with different rules. The console half is ordinary authenticated
workspace API. The serving half is public, unauthenticated, and hands a
browser somebody's generated JavaScript, which is the part worth reading
carefully.

**A built app is untrusted code.** It was written by a model from a sentence a
person typed, and it is served under a link they can send to anyone. Two things
keep that from being an attack on the console:

1. `Content-Security-Policy: sandbox allow-scripts allow-forms` gives the page
   an opaque origin. No `allow-same-origin`, so it cannot read cookies or
   storage belonging to the host it is served from, whatever that host is.
2. `BASIVO_APPS_ORIGIN` should point somewhere that is not the console, and
   the share link is built from it. The sandbox means a deployment that has
   not set one yet is not immediately unsafe; setting it is still the right
   thing, and the setting is where that is written down.

**The link is a derived capability.** An HMAC of the project id under
`SECRET_KEY`, compared in constant time, with a uniform 404 when it does not
match. Nothing to store, nothing to administer, and rotating the key revokes
every link at once.
"""

from __future__ import annotations

import hmac
import io
import tarfile
import uuid
import zipfile
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.appbuilder import service, serving
from basivo_orch.appbuilder.models import AppProject, AppTurn, AppVersion
from basivo_orch.appbuilder.serving import preview_url, project_token, public_url
from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.db import get_async_session
from basivo_orch.flows.models import Artifact

router = APIRouter(prefix="/orgs/{organization_id}/apps", tags=["apps"])

#: Preview links, private to whoever holds the token.
public = APIRouter(prefix="/p", tags=["apps"])
#: Published apps, at an address a person can read aloud.
sites = APIRouter(prefix="/s", tags=["apps"])


# ---------------------------------------------------------------------------
# What the console reads
# ---------------------------------------------------------------------------


class ProjectWrite(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=160)
    engine: str = Field(default="auto", max_length=32)
    credential_id: str = Field(default="", max_length=64)
    provider: str = Field(default="anthropic", max_length=48)
    model: str = Field(default="", max_length=160)


class Message(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=8000)


class VersionRead(BaseModel):
    id: uuid.UUID
    version: int
    engine: str
    created_at: Any
    url: str
    published: bool


class TurnRead(BaseModel):
    id: uuid.UUID
    prompt: str
    reply: str
    error: str
    status: str
    run_id: uuid.UUID | None
    version: int | None
    created_at: Any


class ProjectRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    engine: str
    created_at: Any
    updated_at: Any
    #: Null until the first message builds something.
    latest_version: int | None
    published_version: int | None
    #: What the preview pane loads: the newest build, at its private address.
    preview_url: str
    #: What a person shares. Answers only while a version is deployed.
    share_url: str
    busy: bool


def _project_read(project: AppProject, versions: list[AppVersion], busy: bool) -> ProjectRead:
    latest = versions[-1] if versions else None
    published = next((v for v in versions if v.id == project.published_version_id), None)
    return ProjectRead(
        id=project.id,
        name=project.name,
        slug=project.slug,
        engine=project.engine,
        created_at=project.created_at,
        updated_at=project.updated_at,
        latest_version=latest.version if latest else None,
        published_version=published.version if published else None,
        preview_url=preview_url(project, latest.version if latest else None),
        share_url=public_url(project),
        busy=busy,
    )


async def _load(
    session: AsyncSession, organization_id: uuid.UUID, project_id: uuid.UUID
) -> AppProject:
    project = await session.get(AppProject, project_id)
    if project is None or project.organization_id != organization_id:
        # Missing and not yours are the same answer, as everywhere else here.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That app does not exist.")
    return project


async def _versions(session: AsyncSession, project: AppProject) -> list[AppVersion]:
    rows = await session.execute(
        select(AppVersion).where(AppVersion.project_id == project.id).order_by(AppVersion.version)
    )
    return list(rows.scalars().all())


async def _busy(session: AsyncSession, project: AppProject) -> bool:
    from basivo_orch.appbuilder.models import TurnStatus

    rows = await session.execute(
        select(AppTurn.id).where(
            AppTurn.project_id == project.id,
            AppTurn.status.in_([TurnStatus.QUEUED, TurnStatus.RUNNING]),
        )
    )
    return rows.scalars().first() is not None


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[ProjectRead]:
    rows = await session.execute(
        select(AppProject)
        .where(AppProject.organization_id == context.organization_id)
        .order_by(AppProject.updated_at.desc())
    )
    out: list[ProjectRead] = []
    for project in rows.scalars().all():
        out.append(
            _project_read(project, await _versions(session, project), await _busy(session, project))
        )
    return out


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectWrite,
    context: OrgContext = Depends(require(Permission.FLOW_CREATE)),
    session: AsyncSession = Depends(get_async_session),
) -> ProjectRead:
    project = await service.create_project(
        session,
        organization_id=context.organization_id,
        name=body.name,
        engine=body.engine,
        credential_id=body.credential_id,
        provider=body.provider,
        model=body.model,
        user_id=context.user.id,
    )
    return _project_read(project, [], False)


@router.get("/{project_id}", response_model=ProjectRead)
async def read_project(
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> ProjectRead:
    project = await _load(session, context.organization_id, project_id)
    return _project_read(project, await _versions(session, project), await _busy(session, project))


@router.get("/{project_id}/turns", response_model=list[TurnRead])
async def list_turns(
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[TurnRead]:
    project = await _load(session, context.organization_id, project_id)
    rows = await session.execute(
        select(AppTurn).where(AppTurn.project_id == project.id).order_by(AppTurn.created_at)
    )
    numbers = {version.id: version.version for version in await _versions(session, project)}
    return [
        TurnRead(
            id=turn.id,
            prompt=turn.prompt,
            reply=turn.reply,
            error=turn.error,
            status=str(turn.status),
            run_id=turn.run_id,
            version=numbers.get(turn.version_id) if turn.version_id else None,
            created_at=turn.created_at,
        )
        for turn in rows.scalars().all()
    ]


@router.get("/{project_id}/versions", response_model=list[VersionRead])
async def list_versions(
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[VersionRead]:
    project = await _load(session, context.organization_id, project_id)
    return [
        VersionRead(
            id=version.id,
            version=version.version,
            engine=version.engine,
            created_at=version.created_at,
            url=preview_url(project, version.version),
            published=version.id == project.published_version_id,
        )
        for version in reversed(await _versions(session, project))
    ]


@router.post("/{project_id}/messages", response_model=TurnRead, status_code=201)
async def send_message(
    body: Message,
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_RUN)),
    session: AsyncSession = Depends(get_async_session),
) -> TurnRead:
    project = await _load(session, context.organization_id, project_id)
    try:
        turn, _run = await service.start_turn(
            session, project=project, message=body.text, user_id=context.user.id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return TurnRead(
        id=turn.id,
        prompt=turn.prompt,
        reply="",
        error="",
        status=str(turn.status),
        run_id=turn.run_id,
        version=None,
        created_at=turn.created_at,
    )


@router.post("/{project_id}/versions/{version_id}/deploy", response_model=ProjectRead)
async def deploy(
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_PUBLISH)),
    session: AsyncSession = Depends(get_async_session),
) -> ProjectRead:
    project = await _load(session, context.organization_id, project_id)
    version = await session.get(AppVersion, version_id)
    if version is None or version.project_id != project.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That version does not exist.")
    await service.publish(session, project=project, version=version)
    return _project_read(project, await _versions(session, project), await _busy(session, project))


@router.post("/{project_id}/versions/{version_id}/restore", response_model=ProjectRead)
async def restore(
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_PUBLISH)),
    session: AsyncSession = Depends(get_async_session),
) -> ProjectRead:
    """Undo: make an older version what the next message starts from."""
    project = await _load(session, context.organization_id, project_id)
    version = await session.get(AppVersion, version_id)
    if version is None or version.project_id != project.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That version does not exist.")
    await service.restore(session, project=project, version=version)
    return _project_read(project, await _versions(session, project), await _busy(session, project))


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_PUBLISH)),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    project = await _load(session, context.organization_id, project_id)
    from basivo_orch.flows.models import Flow

    if project.flow_id and (flow := await session.get(Flow, project.flow_id)):
        # The project's hidden flow goes with it, along with its runs.
        await session.delete(flow)
    await session.delete(project)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Unpublish, and the code itself
# ---------------------------------------------------------------------------


@router.post("/{project_id}/unpublish", response_model=ProjectRead)
async def unpublish(
    project_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_PUBLISH)),
    session: AsyncSession = Depends(get_async_session),
) -> ProjectRead:
    """Take the public address down. Versions stay; the link answers not live."""
    project = await _load(session, context.organization_id, project_id)
    await service.unpublish(session, project=project)
    return _project_read(project, await _versions(session, project), await _busy(session, project))


@router.get("/{project_id}/versions/{version_id}/source.zip", include_in_schema=False)
async def download_source(
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.FLOW_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """The project as it was at this version, ready for `npm install`.

    A zip rather than the stored tar, because the person downloading it is as
    likely to be on Windows as not, and the whole point of handing the code
    over is that they can open it without asking how.
    """
    project = await _load(session, context.organization_id, project_id)
    version = await session.get(AppVersion, version_id)
    if version is None or version.project_id != project.id or version.source_artifact_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That version does not exist.")
    artifact = await session.get(Artifact, version.source_artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That version does not exist.")

    folder = f"{project.slug}-v{version.version}"
    body = _zip_of(artifact.data, folder, project.name)
    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{folder}.zip"',
            "Cache-Control": "private, no-store",
        },
    )


README = """# {name}

Built with the basivo App Builder. This is the whole project: React 19,
TypeScript, Tailwind v4 and Vite, with `motion` and `lucide-react` available.

    npm install
    npm run dev      # a development server with hot reload
    npm run build    # the production build, in dist/

Everything you asked for lives in `src/`. `index.html` is the page shell.
"""


def _zip_of(archive: bytes, folder: str, name: str) -> bytes:
    """The stored tar as a zip with one top-level folder and a README."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar,
        zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle,
    ):
        for member in tar.getmembers():
            if not member.isfile() or ".." in member.name:
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                bundle.writestr(f"{folder}/{member.name.strip('/')}", handle.read())
        bundle.writestr(f"{folder}/README.md", README.format(name=name))
    return out.getvalue()


# ---------------------------------------------------------------------------
# The site itself
# ---------------------------------------------------------------------------


@public.get("/{project_id}/{token}", include_in_schema=False)
@public.get("/{project_id}/{token}/{path:path}", include_in_schema=False)
async def preview(
    request: Request,
    project_id: uuid.UUID,
    token: Annotated[str, Path(max_length=64)],
    path: str = "",
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Any version of an app, to whoever holds the project's token."""
    if not hmac.compare_digest(token, project_token(project_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    project = await session.get(AppProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if redirect := serving.directory_redirect(request, path):
        return redirect

    wanted, inside = serving.split_version(path)
    if wanted is None:
        version = await serving.published_version(session, project)
        if version is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "This app has not been published yet.")
        return await serving.serve(
            request, session, version=version, inside=inside, immutable=False
        )

    version = await serving.version_by_number(session, project, wanted)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return await serving.serve(request, session, version=version, inside=inside, immutable=True)


@sites.get("/{public_slug}", include_in_schema=False)
@sites.get("/{public_slug}/{path:path}", include_in_schema=False)
async def published(
    request: Request,
    public_slug: Annotated[str, Path(max_length=80)],
    path: str = "",
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """The deployed app, to anyone at all."""
    rows = await session.execute(select(AppProject).where(AppProject.public_slug == public_slug))
    project = rows.scalars().first()
    if project is None:
        raise serving.not_live()
    if redirect := serving.directory_redirect(request, path):
        return redirect
    version = await serving.published_version(session, project)
    if version is None:
        raise serving.not_live()
    return await serving.serve(request, session, version=version, inside=path, immutable=False)
