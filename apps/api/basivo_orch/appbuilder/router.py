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

import hashlib
import hmac
import io
import os
import tarfile
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.appbuilder import service
from basivo_orch.appbuilder.models import AppProject, AppTurn, AppVersion
from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.db import get_async_session
from basivo_orch.flows.models import Artifact

router = APIRouter(prefix="/orgs/{organization_id}/apps", tags=["apps"])

#: The public half. No prefix under the org, because a share link should be
#: short and should not name the workspace that made it.
public = APIRouter(prefix="/p", tags=["apps"])

#: How a built site is served. Anything not in this list is served as bytes
#: with a type a browser will not execute.
CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

#: An opaque origin for the page, whatever host serves it. Without
#: `allow-same-origin` the document cannot touch cookies or storage of the
#: host, which is what makes serving generated code survivable.
#:
#: `frame-ancestors` is here because the page is meant to be framed: the
#: builder shows it beside the chat. It also replaces `X-Frame-Options`, which
#: the security middleware leaves off once a route has stated this.
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals; frame-ancestors *"


def project_token(project_id: uuid.UUID) -> str:
    """The share link's secret half. Derived, never stored."""
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"app-site:{project_id}".encode(), hashlib.sha256).hexdigest()[:32]


def apps_origin() -> str:
    """Where built apps are served from.

    Its own host in any real deployment: generated JavaScript and the console
    should not share an origin even with the sandbox in place. Unset, it falls
    back to this API rather than to the console, because this API is what
    actually answers `/p/...`; pointing at the console gives a link that loads
    the console's own single page app instead of somebody's site.
    """
    configured = os.environ.get("BASIVO_APPS_ORIGIN", "").strip().rstrip("/")
    return configured or str(get_auth_settings().public_base_url).rstrip("/")


def site_url(project: AppProject, version: int | None = None) -> str:
    """The address of a built app, always ending in a slash.

    The slash is load bearing. A built page asks for `./assets/app.js`, and
    from `/p/<id>/<token>/v3` that resolves to `/p/<id>/<token>/assets/app.js`,
    one directory too high, so the page loads and nothing on it works. From
    `/p/<id>/<token>/v3/` it resolves inside the version. The route redirects
    the unslashed form rather than trusting every caller to remember.
    """
    path = f"/p/{project.id}/{project_token(project.id)}"
    return f"{apps_origin()}{path}" + (f"/v{version}/" if version else "/")


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
    #: What the preview pane loads, and what a person shares. The same address
    #: with and without a version number.
    preview_url: str
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
        preview_url=site_url(project, latest.version if latest else None),
        share_url=site_url(project),
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
            url=site_url(project, version.version),
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
# The site itself
# ---------------------------------------------------------------------------


@public.get("/{project_id}/{token}", include_in_schema=False)
@public.get("/{project_id}/{token}/{path:path}", include_in_schema=False)
async def site(
    request: Request,
    project_id: uuid.UUID,
    token: Annotated[str, Path(max_length=64)],
    path: str = "",
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """One file of a built app, to anyone holding the link."""
    if not hmac.compare_digest(token, project_token(project_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    project = await session.get(AppProject, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    # A directory has to end in a slash or the page's own relative asset
    # requests climb out of it. Redirect rather than serve something that
    # half works.
    if _is_directory(path) and not request.url.path.endswith("/"):
        return RedirectResponse(request.url.path + "/", status_code=308)

    wanted, inside = _split_version(path)
    version = await _serving_version(session, project, wanted)
    if version is None or version.build_artifact_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This app has not been published yet." if wanted is None else "Not found.",
        )

    artifact = await session.get(Artifact, version.build_artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    name = inside or "index.html"
    body = _file_from(artifact.data, name)
    if body is None:
        # A single page app: an unknown path is a route inside it, not a
        # missing file, so the page itself answers.
        body = _file_from(artifact.data, "index.html")
        name = "index.html"
    if body is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    suffix = name[name.rfind(".") :].lower() if "." in name else ""
    return Response(
        content=body,
        media_type=CONTENT_TYPES.get(suffix, "application/octet-stream"),
        headers={
            "Content-Security-Policy": SANDBOX,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            # A version is immutable; the published address is not.
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if wanted is not None
                else "no-cache, must-revalidate"
            ),
            "Cross-Origin-Resource-Policy": "cross-origin",
            # The sandbox gives the document an opaque origin, so the page's
            # own script and stylesheet arrive here as cross-origin requests
            # from "null". Without this they are blocked and the page renders
            # as an empty white box, which is exactly what a person would
            # report as "the preview is broken".
            "Access-Control-Allow-Origin": "*",
        },
    )


def _is_directory(path: str) -> bool:
    """True for the site root and for a bare version, which name directories."""
    head, _, rest = path.partition("/")
    return not path or (not rest and head.startswith("v") and head[1:].isdigit())


def _split_version(path: str) -> tuple[int | None, str]:
    """`v3/assets/app.js` is version three's file; `assets/app.js` is the deployed one."""
    head, _, rest = path.partition("/")
    if head.startswith("v") and head[1:].isdigit():
        return int(head[1:]), rest
    return None, path


async def _serving_version(
    session: AsyncSession, project: AppProject, wanted: int | None
) -> AppVersion | None:
    if wanted is None:
        if project.published_version_id is None:
            return None
        return await session.get(AppVersion, project.published_version_id)
    rows = await session.execute(
        select(AppVersion).where(AppVersion.project_id == project.id, AppVersion.version == wanted)
    )
    return rows.scalars().first()


def _file_from(archive: bytes, name: str) -> bytes | None:
    """One file out of a built site.

    The whole site is one gzipped tar of a few hundred kilobytes, so this
    reads it per request rather than keeping a cache to invalidate. When that
    stops being true, the answer is object storage, not a cache here.
    """
    name = name.strip("/")
    if not name or ".." in name:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = tar.getmember(name)
            if not member.isfile():
                return None
            handle = tar.extractfile(member)
            return handle.read() if handle else None
    except (KeyError, tarfile.TarError):
        return None
