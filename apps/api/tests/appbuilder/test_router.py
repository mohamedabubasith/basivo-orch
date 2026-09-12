"""Projects over HTTP, and the two addresses a built app answers at.

A preview is private and tokened; a published app is public and named. What is
tested here is that the token is unguessable, the page is sandboxed, a version
address serves that version whatever is deployed, unpublishing takes the public
address down at once, and serving a hundred people costs one database read.
"""

from __future__ import annotations

import io
import tarfile
import uuid
import zipfile

import pytest
from fastapi import HTTPException

from basivo_orch.appbuilder import router as api
from basivo_orch.appbuilder import service, serving
from basivo_orch.appbuilder.models import AppVersion
from basivo_orch.flows.models import Artifact

pytestmark = pytest.mark.anyio


def _request(path: str, headers: dict[str, str] | None = None):
    """The little that the serving routes read off a request."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "scheme": "http",
            "server": ("localhost", 8000),
        }
    )


def _site(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
    return buffer.getvalue()


def _context(organization):
    """The little of an authenticated caller that these routes read."""
    from basivo_orch.auth.authz import OrgContext, Permission, Role
    from basivo_orch.auth.models import User

    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified here; the gate runs before the route.
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def _built(session, organization, project, number: int, body: bytes) -> AppVersion:
    build = Artifact(
        organization_id=organization.id,
        filename="site.tar.gz",
        content_type="application/gzip",
        size_bytes=len(body),
        data=_site({"index.html": body, "assets/app.js": b"console.log(1)"}),
    )
    source = Artifact(
        organization_id=organization.id,
        filename="source.tar.gz",
        content_type="application/gzip",
        size_bytes=1,
        data=_site({"package.json": b'{"name": "app"}', "src/App.tsx": b"export default 1"}),
    )
    session.add_all([build, source])
    await session.flush()
    version = AppVersion(
        project_id=project.id,
        version=number,
        build_artifact_id=build.id,
        source_artifact_id=source.id,
        engine="opencode",
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


@pytest.fixture(autouse=True)
def _cold_cache():
    serving.SITES.clear()
    yield
    serving.SITES.clear()


# ---------------------------------------------------------------------------
# The preview address
# ---------------------------------------------------------------------------


async def test_a_version_address_serves_that_version_whatever_is_deployed(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Menu")
    one = await _built(session, organization, project, 1, b"<h1>one</h1>")
    await _built(session, organization, project, 2, b"<h1>two</h1>")
    token = serving.project_token(project.id)

    # Nothing deployed: the preview root says so rather than leaking the
    # newest build, but every version is reachable by number.
    with pytest.raises(HTTPException) as refused:
        await api.preview(_request("/p/x/y/"), project.id, token, "", session=session)
    assert refused.value.status_code == 404
    assert "not been published" in refused.value.detail

    two = await api.preview(_request("/p/x/y/v2/"), project.id, token, "v2/", session=session)
    assert two.body == b"<h1>two</h1>"

    await service.publish(session, project=project, version=one)
    root = await api.preview(_request("/p/x/y/"), project.id, token, "", session=session)
    assert root.body == b"<h1>one</h1>"


async def test_the_page_runs_in_a_sandbox_with_no_access_to_its_host(session, organization):
    """Generated code must not be able to read the cookies of whatever host
    serves it. No allow-same-origin means an opaque origin."""
    project = await service.create_project(session, organization_id=organization.id, name="Shop")
    await _built(session, organization, project, 1, b"<h1>hi</h1>")

    served = await api.preview(
        _request("/p/x/y/v1/"),
        project.id,
        serving.project_token(project.id),
        "v1/",
        session=session,
    )
    policy = served.headers["content-security-policy"]
    assert policy.startswith("sandbox ")
    assert "allow-same-origin" not in policy
    assert "frame-ancestors" in policy
    assert served.headers["x-content-type-options"] == "nosniff"
    # The sandbox gives the page an opaque origin, so its own script arrives
    # as a cross-origin request from "null" and needs this to load at all.
    assert served.headers["access-control-allow-origin"] == "*"


async def test_a_wrong_token_is_a_404_and_not_a_hint(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Blog")
    await _built(session, organization, project, 1, b"<h1>hi</h1>")

    for project_id in (project.id, uuid.uuid4()):
        with pytest.raises(HTTPException) as refused:
            await api.preview(_request("/p/x/y/v1/"), project_id, "0" * 32, "v1/", session=session)
        assert refused.value.status_code == 404
        assert refused.value.detail == "Not found."


async def test_an_unknown_path_falls_back_to_the_page_and_assets_are_typed(session, organization):
    """A single page app owns its routes. /about is the page, not a 404."""
    project = await service.create_project(session, organization_id=organization.id, name="Docs")
    await _built(session, organization, project, 1, b"<h1>hi</h1>")
    token = serving.project_token(project.id)

    page = await api.preview(
        _request("/p/x/y/v1/about"), project.id, token, "v1/about", session=session
    )
    assert page.body == b"<h1>hi</h1>"
    asset = await api.preview(
        _request("/p/x/y/v1/assets/app.js"), project.id, token, "v1/assets/app.js", session=session
    )
    assert asset.body == b"console.log(1)"
    assert asset.headers["content-type"].startswith("text/javascript")
    assert "immutable" in asset.headers["cache-control"]

    climb = await api.preview(
        _request("/p/x/y/v1/../../etc/passwd"),
        project.id,
        token,
        "v1/../../etc/passwd",
        session=session,
    )
    assert climb.body == b"<h1>hi</h1>", "a path out of the archive is just an unknown route"


async def test_a_directory_without_its_slash_redirects(session, organization):
    """`/v1` and `/v1/` differ: relative assets from the first climb out."""
    project = await service.create_project(session, organization_id=organization.id, name="Slash")
    await _built(session, organization, project, 1, b"<h1>hi</h1>")
    token = serving.project_token(project.id)

    redirect = await api.preview(
        _request(f"/p/{project.id}/{token}/v1"), project.id, token, "v1", session=session
    )
    assert redirect.status_code == 308
    assert redirect.headers["location"].endswith("/v1/")

    assert serving.preview_url(project, 1).endswith("/v1/")
    assert serving.public_url(project).endswith(f"/{project.public_slug}/")


# ---------------------------------------------------------------------------
# The public address
# ---------------------------------------------------------------------------


async def test_a_published_app_has_a_name_you_can_read_aloud(session, organization):
    project = await service.create_project(
        session, organization_id=organization.id, name="Sunrise Bakery"
    )
    assert project.public_slug.startswith("sunrise-bakery-")
    suffix = project.public_slug.rsplit("-", 1)[1]
    assert len(suffix) == 4 and set(suffix) <= set(serving.SUFFIX_ALPHABET)

    other = await service.create_project(
        session, organization_id=organization.id, name="Sunrise Bakery"
    )
    assert other.public_slug != project.public_slug


async def test_deploy_makes_it_live_and_unpublish_takes_it_down(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Cafe")
    version = await _built(session, organization, project, 1, b"<h1>live</h1>")
    slug = project.public_slug

    with pytest.raises(HTTPException) as before:
        await api.published(_request(f"/s/{slug}/"), slug, "", session=session)
    assert before.value.status_code == 404 and before.value.detail == "This app is not live."

    await service.publish(session, project=project, version=version)
    served = await api.published(_request(f"/s/{slug}/"), slug, "", session=session)
    assert served.body == b"<h1>live</h1>"
    # The page itself is what the next deploy replaces, so a browser must ask.
    assert "no-cache" in served.headers["cache-control"]
    # Its hashed assets never change under their names.
    asset = await api.published(
        _request(f"/s/{slug}/assets/app.js"), slug, "assets/app.js", session=session
    )
    assert "immutable" in asset.headers["cache-control"]

    await service.unpublish(session, project=project)
    with pytest.raises(HTTPException) as after:
        await api.published(_request(f"/s/{slug}/"), slug, "", session=session)
    assert after.value.detail == "This app is not live."
    # The versions are all still there; only the pointer went.
    assert (await serving.version_by_number(session, project, 1)) is not None


async def test_a_dedicated_domain_serves_apps_at_its_root(monkeypatch):
    """`https://apps.example.com/sunrise-k3d9/` is the address on a business
    card. The host middleware maps it onto the `/s/` route; on the API's own
    host it does nothing."""
    monkeypatch.setenv("BASIVO_APPS_ORIGIN", "https://apps.example.com")
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])

    middleware = serving.SitesHostMiddleware(inner)
    await middleware(
        {
            "type": "http",
            "path": "/sunrise-k3d9/assets/app.js",
            "headers": [(b"host", b"apps.example.com")],
        },
        None,
        None,
    )
    await middleware(
        {"type": "http", "path": "/p/x/y/v1/", "headers": [(b"host", b"apps.example.com")]},
        None,
        None,
    )
    await middleware(
        {"type": "http", "path": "/api/v1/orgs", "headers": [(b"host", b"localhost:8000")]},
        None,
        None,
    )
    assert seen == ["/s/sunrise-k3d9/assets/app.js", "/p/x/y/v1/", "/api/v1/orgs"]

    class Project:
        public_slug = "sunrise-k3d9"

    assert serving.public_url(Project()) == "https://apps.example.com/sunrise-k3d9/"  # type: ignore[arg-type]

    monkeypatch.delenv("BASIVO_APPS_ORIGIN")
    assert serving.public_url(Project()) == "http://localhost:8000/s/sunrise-k3d9/"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cheap to serve
# ---------------------------------------------------------------------------


async def test_a_hundred_visitors_cost_one_read_and_returning_browsers_get_304(
    session, organization, monkeypatch
):
    project = await service.create_project(session, organization_id=organization.id, name="Busy")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)
    slug = project.public_slug

    reads = 0
    original = serving.unpack_site

    def counting(archive: bytes):
        nonlocal reads
        reads += 1
        return original(archive)

    monkeypatch.setattr(serving, "unpack_site", counting)

    first = await api.published(_request(f"/s/{slug}/"), slug, "", session=session)
    for _ in range(99):
        await api.published(
            _request(f"/s/{slug}/assets/app.js"), slug, "assets/app.js", session=session
        )
    assert reads == 1, "the build is unpacked once per process, not once per request"

    again = await api.published(
        _request(f"/s/{slug}/", {"If-None-Match": first.headers["etag"]}), slug, "", session=session
    )
    assert again.status_code == 304
    # A browser applies a 304's headers to the document it kept. One without
    # the sandbox policy would be given the API's by the middleware, and its
    # frame-ancestors 'none' blocks the preview the second time it is shown.
    assert again.headers["content-security-policy"] == first.headers["content-security-policy"]
    assert again.headers["access-control-allow-origin"] == "*"


def test_the_cache_is_bounded_and_forgets_the_least_recently_served():
    cache = serving.SiteCache(limit=100)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.put(a, {"index.html": b"x" * 40})
    cache.put(b, {"index.html": b"y" * 40})
    assert cache.get(a) is not None  # a is now the most recently served
    cache.put(c, {"index.html": b"z" * 40})  # over the limit: b goes, a stays
    assert cache.get(b) is None and cache.get(a) is not None and cache.get(c) is not None
    cache.put(uuid.uuid4(), {"big": b"w" * 200})  # larger than the whole cache: skipped
    assert cache.size <= 100


# ---------------------------------------------------------------------------
# The code, and the project's plumbing
# ---------------------------------------------------------------------------


async def test_the_code_downloads_as_a_zip_anyone_can_open(session, organization):
    project = await service.create_project(
        session, organization_id=organization.id, name="Take Home"
    )
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    source = await session.get(Artifact, version.source_artifact_id)

    body = api._zip_of(source.data, f"{project.slug}-v1", project.name)
    with zipfile.ZipFile(io.BytesIO(body)) as bundle:
        names = bundle.namelist()
        assert f"{project.slug}-v1/package.json" in names
        assert f"{project.slug}-v1/src/App.tsx" in names
        readme = bundle.read(f"{project.slug}-v1/README.md").decode()
    assert "npm install" in readme and "Take Home" in readme


async def test_the_download_carries_the_pictures_that_are_not_in_the_tree(session, organization):
    """Uploads live once in the database, so the zip is where they are put
    back: a project that builds without the person's own photographs is not
    their project."""
    project = await service.create_project(session, organization_id=organization.id, name="Shop")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    source = await session.get(Artifact, version.source_artifact_id)
    await service.add_asset(
        session, project=project, filename="front.png", data=b"\x89PNG\r\n\x1a\nphoto"
    )

    body = api._zip_of(
        source.data,
        f"{project.slug}-v1",
        project.name,
        await service.asset_files(session, project),
    )
    with zipfile.ZipFile(io.BytesIO(body)) as bundle:
        assert f"{project.slug}-v1/public/uploads/front.png" in bundle.namelist()


async def test_the_code_tab_reads_a_version_and_lists_what_it_cannot_show(session, organization):
    """Text arrives with the listing, because a tree that needs a request per
    click feels broken. A photograph is listed with its size and no text."""
    project = await service.create_project(session, organization_id=organization.id, name="Code")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.add_asset(
        session, project=project, filename="front.png", data=b"\x89PNG\r\n\x1a\nphoto"
    )

    files = await api.read_source(
        project.id, version.id, context=_context(organization), session=session
    )
    by_path = {file.path: file for file in files}

    assert "src/App.tsx" in by_path and by_path["src/App.tsx"].text
    assert by_path["package.json"].text.startswith("{")
    upload = by_path["public/uploads/front.png"]
    assert upload.text == "" and upload.size_bytes > 0


async def test_one_message_at_a_time(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Queue")
    await service.start_turn(session, project=project, message="a page")
    with pytest.raises(ValueError, match="still working"):
        await service.start_turn(session, project=project, message="and a footer")


async def test_a_project_owns_a_hidden_flow_with_one_node(session, organization):
    """The turn queue is the run queue. That is the whole trick, so it is pinned."""
    from basivo_orch.flows.models import Flow, FlowVersion
    from basivo_orch.flows.service import list_flows

    project = await service.create_project(session, organization_id=organization.id, name="Cafe")
    flow = await session.get(Flow, project.flow_id)
    assert flow is not None and flow.system is True
    assert await list_flows(session, organization_id=organization.id) == []

    version = await session.get(FlowVersion, flow.published_version_id)
    assert [node["type"] for node in version.graph["nodes"]] == ["trigger.manual", "app.build"]
    build = next(n for n in version.graph["nodes"] if n["type"] == "app.build")
    assert build["config"]["project_id"] == str(project.id)


async def test_deploying_answers_with_the_project_it_just_changed(session, organization):
    """`updated_at` is written by the database, so reading it after the update
    must not be a lazy load: in an async request that is a MissingGreenlet."""
    project = await service.create_project(session, organization_id=organization.id, name="Deploy")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")

    await service.publish(session, project=project, version=version)
    assert api._project_read(project, [version], False).published_version == 1
    await service.unpublish(session, project=project)
    assert api._project_read(project, [version], False).published_version is None
    await service.restore(session, project=project, version=version)
    assert project.updated_at is not None


async def test_a_turn_whose_run_died_is_closed_so_the_next_message_can_go(session, organization):
    """A worker that dies mid-turn never calls finish. Without reconciling
    against the run, the project says "working" until somebody edits the
    database, which is the one failure a person cannot recover from."""
    from basivo_orch.appbuilder.models import TurnStatus
    from basivo_orch.flows.models import Run, RunStatus

    project = await service.create_project(session, organization_id=organization.id, name="Stuck")
    turn, run = await service.start_turn(session, project=project, message="a page")
    assert await service.busy(session, project) is True

    stored = await session.get(Run, run.id)
    stored.status = RunStatus.FAILED
    stored.error = "Node 'build' failed: OpenCode did not finish within 600s."
    await session.commit()

    assert await service.busy(session, project) is False
    await session.refresh(turn)
    assert turn.status == TurnStatus.FAILED
    assert turn.error.startswith("OpenCode did not finish"), "the node prefix is for the run log"

    # And the next message goes through.
    await service.start_turn(session, project=project, message="try again, smaller")


async def test_an_app_can_be_pointed_at_your_own_credential(session, organization):
    """The failure message tells people to use their own key when the included
    agent will not answer, so there has to be a way to do it after the app
    exists. Past turns keep the graph they ran: the change is a new version."""
    project = await service.create_project(session, organization_id=organization.id, name="Menu")
    credential = str(uuid.uuid4())

    read = await api.set_model(
        project.id,
        api.ModelWrite(
            engine="claude_code",
            credential_id=credential,
            provider="anthropic",
            model="claude-sonnet-5",
        ),
        context=_context(organization),
        session=session,
    )

    assert read.credential_id == credential
    assert read.engine == "claude_code"
    config = await service.build_config(session, project)
    assert config["credential_id"] == credential
    assert config["model"] == "claude-sonnet-5"
    # And the node still knows which project it builds.
    assert config["project_id"] == str(project.id)


async def test_the_model_cannot_be_changed_under_a_running_message(
    session, organization, monkeypatch
):
    """Half a turn on one agent and half on another is not a build anybody can
    reason about, so the answer while it is working is wait."""
    project = await service.create_project(session, organization_id=organization.id, name="Menu")
    monkeypatch.setattr(service, "busy", lambda *_args, **_kwargs: _true())

    with pytest.raises(HTTPException) as caught:
        await api.set_model(
            project.id,
            api.ModelWrite(engine="opencode"),
            context=_context(organization),
            session=session,
        )
    assert caught.value.status_code == 409


async def _true() -> bool:
    return True
