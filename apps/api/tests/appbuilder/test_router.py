"""Projects over HTTP, and the public site a link leads to.

The interesting half is the public one. It serves code a model wrote to
whoever holds a link, so what is tested here is the link being unguessable,
the page being sandboxed, and a version address serving that version rather
than whatever was deployed last.
"""

from __future__ import annotations

import io
import tarfile
import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.appbuilder import router as api
from basivo_orch.appbuilder import service
from basivo_orch.appbuilder.models import AppVersion
from basivo_orch.flows.models import Artifact

pytestmark = pytest.mark.anyio


def _request(path: str):
    """The little that the serving route reads off a request."""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
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


async def _built(session, organization, project, number: int, body: bytes) -> AppVersion:
    artifact = Artifact(
        organization_id=organization.id,
        filename="site.tar.gz",
        content_type="application/gzip",
        size_bytes=len(body),
        data=_site({"index.html": body, "assets/app.js": b"console.log(1)"}),
    )
    session.add(artifact)
    await session.flush()
    version = AppVersion(
        project_id=project.id, version=number, build_artifact_id=artifact.id, engine="opencode"
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def test_a_share_link_serves_only_what_was_deployed(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Menu")
    one = await _built(session, organization, project, 1, b"<h1>one</h1>")
    two = await _built(session, organization, project, 2, b"<h1>two</h1>")
    token = api.project_token(project.id)

    # Nothing is deployed yet, so the share link says so rather than leaking
    # the newest build.
    with pytest.raises(HTTPException) as refused:
        await api.site(_request("/p/x/y/"), project.id, token, "", session=session)
    assert refused.value.status_code == 404
    assert "not been published" in refused.value.detail

    await service.publish(session, project=project, version=one)
    served = await api.site(_request("/p/x/y/"), project.id, token, "", session=session)
    assert served.body == b"<h1>one</h1>"
    assert served.headers["content-type"].startswith("text/html")

    # A version address serves that version whatever is deployed, which is
    # what makes a preview of work in progress possible.
    preview = await api.site(_request("/p/x/y/"), project.id, token, "v2/", session=session)
    assert preview.body == b"<h1>two</h1>"

    await service.publish(session, project=project, version=two)
    assert (await api.site(_request("/p/x/y/"), project.id, token, "", session=session)).body == b"<h1>two</h1>"


async def test_the_page_runs_in_a_sandbox_with_no_access_to_its_host(session, organization):
    """Generated code must not be able to read the cookies of whatever host
    serves it. No allow-same-origin means an opaque origin, which is the
    thing that makes serving it survivable at all."""
    project = await service.create_project(session, organization_id=organization.id, name="Shop")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)

    served = await api.site(_request("/p/x/y/"), project.id, api.project_token(project.id), "", session=session)
    policy = served.headers["content-security-policy"]
    assert policy.startswith("sandbox ")
    assert "allow-same-origin" not in policy
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_a_wrong_token_is_a_404_and_not_a_hint(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Blog")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)

    with pytest.raises(HTTPException) as refused:
        await api.site(_request("/p/x/y/"), project.id, "0" * 32, "", session=session)
    assert refused.value.status_code == 404
    assert refused.value.detail == "Not found."

    # And a project nobody has heard of answers exactly the same way.
    with pytest.raises(HTTPException) as missing:
        await api.site(_request("/p/x/y/"), uuid.uuid4(), "0" * 32, "", session=session)
    assert missing.value.status_code == 404


async def test_an_unknown_path_falls_back_to_the_page_itself(session, organization):
    """A single page app owns its routes. /about is the page, not a 404."""
    project = await service.create_project(session, organization_id=organization.id, name="Docs")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)
    token = api.project_token(project.id)

    assert (await api.site(_request("/p/x/y/"), project.id, token, "about", session=session)).body == b"<h1>hi</h1>"
    asset = await api.site(_request("/p/x/y/"), project.id, token, "assets/app.js", session=session)
    assert asset.body == b"console.log(1)"
    assert asset.headers["content-type"].startswith("text/javascript")


async def test_a_path_climbing_out_of_the_archive_is_refused(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Safe")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)

    served = await api.site(
        _request("/p/x/y/"),
        project.id, api.project_token(project.id), "../../etc/passwd", session=session
    )
    # It cannot escape, so it falls through to the page like any other unknown
    # path rather than reading a file.
    assert served.body == b"<h1>hi</h1>"


async def test_one_message_at_a_time(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Queue")
    await service.start_turn(session, project=project, message="a page")

    with pytest.raises(ValueError, match="still working"):
        await service.start_turn(session, project=project, message="and a footer")


async def test_a_project_owns_a_hidden_flow_with_one_node(session, organization):
    """The turn queue is the run queue. That is the whole trick, so it is
    pinned: a project without its flow has nothing to run it."""
    from basivo_orch.flows.models import Flow, FlowVersion

    project = await service.create_project(session, organization_id=organization.id, name="Cafe")
    flow = await session.get(Flow, project.flow_id)
    assert flow is not None and flow.system is True

    version = await session.get(FlowVersion, flow.published_version_id)
    assert version is not None
    types = [node["type"] for node in version.graph["nodes"]]
    assert types == ["trigger.manual", "app.build"]
    build = next(n for n in version.graph["nodes"] if n["type"] == "app.build")
    assert build["config"]["project_id"] == str(project.id)


async def test_two_projects_with_one_name_get_their_own_addresses(session, organization):
    first = await service.create_project(session, organization_id=organization.id, name="Portfolio")
    second = await service.create_project(
        session, organization_id=organization.id, name="Portfolio"
    )
    assert first.slug != second.slug
    assert api.project_token(first.id) != api.project_token(second.id)


async def test_a_project_flow_stays_out_of_the_flows_list(session, organization):
    """The canvas exists for the engine, not for a person to open."""
    from basivo_orch.flows.service import list_flows

    await service.create_project(session, organization_id=organization.id, name="Hidden")
    assert await list_flows(session, organization_id=organization.id) == []


def test_a_share_link_points_at_whatever_serves_it(monkeypatch):
    """Unset, the link must point at this API, which is what answers /p.

    Pointing at the console gives a link that loads the console's own single
    page app, which is the bug this pins.
    """
    monkeypatch.delenv("BASIVO_APPS_ORIGIN", raising=False)
    assert api.apps_origin() == "http://localhost:8000"

    monkeypatch.setenv("BASIVO_APPS_ORIGIN", "https://apps.basivo.in/")
    assert api.apps_origin() == "https://apps.basivo.in"


async def test_a_sandboxed_page_may_load_its_own_assets(session, organization):
    """The sandbox gives the page an opaque origin, so its own script arrives
    as a cross-origin request from "null". Without this header the browser
    blocks it and the preview is a white box with no error a person can see."""
    project = await service.create_project(session, organization_id=organization.id, name="Assets")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)

    served = await api.site(
        _request("/p/x/y/"),
        project.id,
        api.project_token(project.id),
        "assets/app.js",
        session=session,
    )
    assert served.headers["access-control-allow-origin"] == "*"


async def test_a_directory_without_its_slash_redirects(session, organization):
    """`/v1` and `/v1/` differ: relative assets from the first climb out of the
    version and land on nothing."""
    project = await service.create_project(session, organization_id=organization.id, name="Slash")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")
    await service.publish(session, project=project, version=version)
    token = api.project_token(project.id)

    redirect = await api.site(
        _request(f"/p/{project.id}/{token}/v1"), project.id, token, "v1", session=session
    )
    assert redirect.status_code == 308
    assert redirect.headers["location"].endswith("/v1/")

    # And the addresses handed out already carry the slash.
    assert api.site_url(project, 1).endswith("/v1/")
    assert api.site_url(project).endswith(f"/{token}/")


async def test_deploying_answers_with_the_project_it_just_changed(session, organization):
    """The row's `updated_at` is written by the database, so reading it back
    after the update must not be a lazy load: in an async request that is a
    MissingGreenlet, and the browser reports the 500 as a CORS failure, which
    sends you looking in entirely the wrong place."""
    project = await service.create_project(session, organization_id=organization.id, name="Deploy")
    version = await _built(session, organization, project, 1, b"<h1>hi</h1>")

    await service.publish(session, project=project, version=version)
    assert project.updated_at is not None
    assert api._project_read(project, [version], False).published_version == 1

    await service.restore(session, project=project, version=version)
    assert project.updated_at is not None
