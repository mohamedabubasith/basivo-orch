"""Serving a built app to the world, fast and from a clean address.

Two kinds of address, on purpose:

**A preview is private.** `/p/<project id>/<token>/v3/` is for the person
building the app and for the pane beside their chat. The token is an HMAC of
the project id under `SECRET_KEY`, so nothing is stored and a wrong token is a
uniform 404. Every version has one of these forever.

**A published app is public.** `/s/<public slug>/`, or on a dedicated apps
domain simply `https://apps.example.com/<public slug>/`. The slug is the app's
name plus four random characters, readable aloud and unique across every
workspace. Deploy points it at a version; Unpublish takes it down and the
address answers "not live" rather than serving the last thing that was there.

**It has to be cheap.** A hundred people opening one app is three hundred
asset requests, and reading a gzipped tar out of Postgres and unpacking it
for each of them would make the database the bottleneck of a static site.
Versions are immutable, so a build is unpacked once per process and kept in a
bounded in-memory cache, every response carries an ETag so a returning browser
gets a 304, and hashed assets are marked immutable so it does not ask at all.
The published `index.html` is the one thing that may change, and it is the one
thing served with `no-cache`.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import secrets
import tarfile
import uuid
from collections import OrderedDict
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from basivo_orch.appbuilder.models import AppProject, AppVersion
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.flows.models import Artifact

#: How a built site is served. Anything not in this list is served as bytes
#: with a type a browser will not execute.
CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
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
    ".webmanifest": "application/manifest+json",
}

#: An opaque origin for the page, whatever host serves it. Without
#: `allow-same-origin` the document cannot touch cookies or storage of the
#: host, which is what makes serving generated code survivable.
#:
#: `frame-ancestors` is here because the page is meant to be framed: the
#: builder shows it beside the chat. It also replaces `X-Frame-Options`, which
#: the security middleware leaves off once a route has stated this.
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals; frame-ancestors *"

#: How much unpacked site the process keeps. A built page is a few hundred
#: kilobytes, so this is hundreds of apps, and the least recently served one
#: goes first when it fills.
CACHE_BYTES = 128 * 1024 * 1024

#: The alphabet for a public slug's suffix: lower case and digits, without the
#: characters people misread when reading an address aloud.
SUFFIX_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


def project_token(project_id: uuid.UUID) -> str:
    """The preview link's secret half. Derived, never stored."""
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"app-site:{project_id}".encode(), hashlib.sha256).hexdigest()[:32]


def apps_origin() -> str:
    """Where built apps are served from.

    Its own host in any real deployment: generated JavaScript and the console
    should not share an origin even with the sandbox in place. Unset, it falls
    back to this API rather than to the console, because this API is what
    actually answers `/p/...` and `/s/...`.
    """
    configured = os.environ.get("BASIVO_APPS_ORIGIN", "").strip().rstrip("/")
    return configured or str(get_auth_settings().public_base_url).rstrip("/")


def dedicated_host() -> str:
    """The apps host, when it is not the API's own. Empty otherwise.

    On a dedicated host the published address is the root of the domain, which
    is what a person expects to read on a business card; on a shared host it
    sits under `/s/` so it cannot collide with the API.
    """
    apps = urlsplit(apps_origin()).netloc.lower()
    api = urlsplit(str(get_auth_settings().public_base_url)).netloc.lower()
    return apps if apps and apps != api else ""


def preview_url(project: AppProject, version: int | None = None) -> str:
    """Always ends in a slash: a page asks for `./assets/app.js`, and from
    `/v3` that climbs one directory too high."""
    path = f"/p/{project.id}/{project_token(project.id)}"
    return f"{apps_origin()}{path}" + (f"/v{version}/" if version else "/")


def public_url(project: AppProject) -> str:
    """The address a person shares. Clean on a dedicated domain."""
    prefix = "" if dedicated_host() else "/s"
    return f"{apps_origin()}{prefix}/{project.public_slug}/"


def new_public_slug(base: str) -> str:
    """`sunrise-bakery-k3d9`: the name, and four characters nobody else has."""
    suffix = "".join(secrets.choice(SUFFIX_ALPHABET) for _ in range(4))
    return f"{base[:60].strip('-') or 'app'}-{suffix}"


class SitesHostMiddleware:
    """On a dedicated apps domain, the root of the host is the sites.

    `https://apps.example.com/sunrise-bakery-k3d9/` reaches this API with a
    path the router does not know. When the request's host is the apps host
    and not the API's, the path is rewritten to the `/s/` route, so one router
    serves both shapes and the person never sees the prefix. Preview links keep
    their own `/p/` prefix on every host.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = dedicated_host()
            if host:
                request_host = next(
                    (
                        value.decode().lower()
                        for name, value in scope.get("headers", [])
                        if name == b"host"
                    ),
                    "",
                )
                path = scope.get("path", "")
                if request_host == host and not path.startswith(("/s/", "/p/")):
                    scope = dict(scope)
                    scope["path"] = "/s" + path
                    scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Files, once per version per process
# ---------------------------------------------------------------------------


class SiteCache:
    """Unpacked builds, most recently served kept, bounded by bytes.

    Correct without invalidation because a version's artifact never changes:
    the key is the artifact id, and a new deploy is a new id. Per process, so
    two API replicas each warm their own, which is fine, because the cost being
    avoided is per request and not per process.
    """

    def __init__(self, limit: int = CACHE_BYTES) -> None:
        self.limit = limit
        self.size = 0
        self._files: OrderedDict[uuid.UUID, dict[str, bytes]] = OrderedDict()

    def get(self, artifact_id: uuid.UUID) -> dict[str, bytes] | None:
        files = self._files.get(artifact_id)
        if files is not None:
            self._files.move_to_end(artifact_id)
        return files

    def put(self, artifact_id: uuid.UUID, files: dict[str, bytes]) -> None:
        weight = sum(len(blob) for blob in files.values())
        if weight > self.limit:
            return
        self._files[artifact_id] = files
        self.size += weight
        while self.size > self.limit and self._files:
            _, evicted = self._files.popitem(last=False)
            self.size -= sum(len(blob) for blob in evicted.values())

    def clear(self) -> None:
        self._files.clear()
        self.size = 0


SITES = SiteCache()


def unpack_site(archive: bytes) -> dict[str, bytes]:
    """Every file in a built site, keyed by its path inside the site."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or ".." in member.name:
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                files[member.name.strip("/")] = handle.read()
    return files


async def site_files(session: AsyncSession, artifact_id: uuid.UUID) -> dict[str, bytes] | None:
    cached = SITES.get(artifact_id)
    if cached is not None:
        return cached
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        return None
    try:
        files = unpack_site(artifact.data)
    except tarfile.TarError:
        return None
    SITES.put(artifact_id, files)
    return files


# ---------------------------------------------------------------------------
# One response
# ---------------------------------------------------------------------------


async def serve(
    request: Request,
    session: AsyncSession,
    *,
    version: AppVersion,
    inside: str,
    immutable: bool,
) -> Response:
    """One file of one version, with the headers that make it cheap and safe."""
    if version.build_artifact_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    files = await site_files(session, version.build_artifact_id)
    if files is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    name = inside.strip("/") or "index.html"
    body = files.get(name)
    if body is None:
        # A single page app owns its routes: an unknown path is a page inside
        # it, not a missing file, so the page itself answers.
        name = "index.html"
        body = files.get(name)
    if body is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")

    # The artifact never changes, so the pair identifies the bytes exactly.
    tag = hashlib.blake2s(name.encode(), digest_size=4).hexdigest()
    etag = f'"{version.build_artifact_id.hex[:16]}-{tag}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    suffix = name[name.rfind(".") :].lower() if "." in name else ""
    # Hashed assets never change under their name, so they are immutable
    # wherever they are addressed from. The published page itself is what a
    # new deploy replaces, so it is the one thing a browser must ask about.
    forever = immutable or name.startswith("assets/")
    return Response(
        content=body,
        media_type=CONTENT_TYPES.get(suffix, "application/octet-stream"),
        headers={
            "ETag": etag,
            "Content-Security-Policy": SANDBOX,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": (
                "public, max-age=31536000, immutable" if forever else "no-cache, must-revalidate"
            ),
            "Cross-Origin-Resource-Policy": "cross-origin",
            # The sandbox gives the document an opaque origin, so the page's
            # own script and stylesheet arrive here as cross-origin requests
            # from "null". Without this they are blocked and the page renders
            # as an empty white box.
            "Access-Control-Allow-Origin": "*",
        },
    )


def directory_redirect(request: Request, path: str) -> Response | None:
    """A directory has to end in a slash or the page's own relative asset
    requests climb out of it. Redirect rather than serve something that half
    works."""
    head, _, rest = path.partition("/")
    is_directory = not path or (not rest and head.startswith("v") and head[1:].isdigit())
    if is_directory and not request.url.path.endswith("/"):
        return RedirectResponse(request.url.path + "/", status_code=308)
    return None


def split_version(path: str) -> tuple[int | None, str]:
    """`v3/assets/app.js` is version three's file; `assets/app.js` is the deployed one."""
    head, _, rest = path.partition("/")
    if head.startswith("v") and head[1:].isdigit():
        return int(head[1:]), rest
    return None, path


async def version_by_number(
    session: AsyncSession, project: AppProject, number: int
) -> AppVersion | None:
    rows = await session.execute(
        select(AppVersion).where(AppVersion.project_id == project.id, AppVersion.version == number)
    )
    return rows.scalars().first()


async def published_version(session: AsyncSession, project: AppProject) -> AppVersion | None:
    if project.published_version_id is None:
        return None
    return await session.get(AppVersion, project.published_version_id)


def not_live() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "This app is not live.")
