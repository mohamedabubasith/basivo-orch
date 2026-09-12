"""Uploading an image, and what the turn does with it.

The interesting part is not the upload, it is where the bytes end up: once in
the database, never in a version, present in the working copy the agent reads,
present in the build a browser loads, and present in the zip somebody
downloads. That chain is what these tests pin, with the real Vite build in the
middle of it.
"""

from __future__ import annotations

import base64
import io
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, UploadFile

from basivo_orch.appbuilder import assets, router, service, turns
from basivo_orch.appbuilder import workspace as ws
from basivo_orch.flows.nodes.engines import EngineResult

pytestmark = pytest.mark.anyio

#: A real one pixel PNG, so the sniffing is tested against a file and not
#: against a made up header.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _names(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {member.name for member in tar.getmembers()}


def test_the_bytes_decide_the_type_and_a_document_is_refused():
    assert assets.sniff(PNG) == ("image/png", ".png")
    assert assets.sniff(b"<svg xmlns='x'></svg>")[0] == "image/svg+xml"
    with pytest.raises(assets.AssetRefused):
        assets.sniff(b"<!doctype html><script>alert(1)</script>")
    with pytest.raises(assets.AssetRefused):
        assets.sniff(b"")


def test_the_name_is_ours_and_cannot_leave_the_uploads_directory():
    assert assets.safe_name("../../etc/passwd", ".png") == "passwd.png"
    assert assets.safe_name("My Logo (final).PNG", ".png") == "my-logo-final.png"
    assert assets.safe_name(".", ".png") == "image.png"


def test_a_second_upload_of_one_name_is_a_second_image():
    assert assets.unique_name("logo.png", set()) == "logo.png"
    assert assets.unique_name("logo.png", {"logo.png"}) == "logo-2.png"
    assert assets.unique_name("logo.png", {"logo.png", "logo-2.png"}) == "logo-3.png"


def test_the_limits_say_which_one_was_hit():
    with pytest.raises(assets.AssetRefused, match="MB"):
        assets.check_room(b"x" * (assets.MAX_ASSET_BYTES + 1), count=0, used=0)
    with pytest.raises(assets.AssetRefused, match="already has"):
        assets.check_room(PNG, count=assets.MAX_PROJECT_ASSETS, used=0)
    with pytest.raises(assets.AssetRefused, match="over"):
        assets.check_room(PNG, count=1, used=assets.MAX_PROJECT_ASSET_BYTES)


async def test_an_upload_is_stored_once_and_offered_to_the_next_turn(session, organization):
    project = await service.create_project(session, organization_id=organization.id, name="Bakery")
    first = await service.add_asset(session, project=project, filename="Shop Front.png", data=PNG)
    second = await service.add_asset(session, project=project, filename="shop front.png", data=PNG)
    assert first.filename == "shop-front.png"
    assert second.filename == "shop-front-2.png"

    files = await service.asset_files(session, project)
    assert set(files) == {
        "public/uploads/shop-front.png",
        "public/uploads/shop-front-2.png",
    }

    assert await service.delete_asset(session, project=project, asset_id=first.id) is True
    assert set(await service.asset_files(session, project)) == {"public/uploads/shop-front-2.png"}


async def test_an_image_is_refused_rather_than_stored_when_it_is_not_an_image(
    session, organization
):
    project = await service.create_project(session, organization_id=organization.id, name="No")
    with pytest.raises(assets.AssetRefused):
        await service.add_asset(
            session, project=project, filename="logo.png", data=b"not a picture"
        )
    assert await service.list_assets(session, project) == []


@dataclass
class ImageAgent:
    """An agent that puts the uploaded picture on the page."""

    name: str = "opencode"
    label: str = "OpenCode (free)"
    free: bool = True
    prompt: str = ""

    def available(self) -> bool:
        return True

    def drives(self, provider: str) -> bool:
        return True

    async def run(self, *, cwd: Path, prompt: str, **kwargs: Any) -> EngineResult:
        self.prompt = prompt
        (cwd / "src" / "App.tsx").write_text(
            "export default function App() {\n"
            '  return <img src="/uploads/shop-front.png" alt="The shop" />;\n'
            "}\n"
        )
        return EngineResult(text="Put your photograph at the top.")


@pytest.mark.skipif(
    not ws.is_installed(), reason="the app template's node_modules is not installed here"
)
async def test_the_picture_reaches_the_agent_and_the_build_but_not_the_version():
    """One copy in the database, one in every build, none in the stored tree."""
    agent = ImageAgent()
    result = await turns.run_turn(
        message="Put the photograph of the shop at the top",
        history=[],
        source=None,
        assets={"public/uploads/shop-front.png": PNG},
        engine=agent,
        workspace=ws.TempWorkspace(),
    )

    assert result.ok, result.error
    # The agent was told the picture exists rather than left to find it.
    assert "/uploads/shop-front.png" in agent.prompt

    # Not in the stored tree: a version carries the page, not the photograph.
    assert not any(name.startswith("public/uploads/") for name in _names(result.source))
    # In the build, because that is what the browser loads.
    assert "uploads/shop-front.png" in _names(result.dist)


def _context(organization):
    from basivo_orch.auth.authz import OrgContext, Permission, Role
    from basivo_orch.auth.models import User

    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified here; the gate runs before the route.
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


def _upload(name: str, blob: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(blob))


async def test_the_route_takes_a_picture_and_refuses_a_document(session, organization):
    """The refusal a person meets is a 413 with the reason, not a 500."""
    project = await service.create_project(session, organization_id=organization.id, name="Route")
    context = _context(organization)

    stored = await router.upload_asset(
        project.id, file=_upload("Front.png", PNG), context=context, session=session
    )
    assert stored.path == "/uploads/front.png"
    assert stored.url.endswith(f"/apps/{project.id}/assets/{stored.id}/file")

    listed = await router.list_assets(project.id, context=context, session=session)
    assert [item.filename for item in listed.items] == ["front.png"]
    assert listed.used_bytes == len(PNG)

    with pytest.raises(HTTPException) as refused:
        await router.upload_asset(
            project.id,
            file=_upload("sneaky.png", b"<!doctype html><script>alert(1)</script>"),
            context=context,
            session=session,
        )
    assert refused.value.status_code == 413
    assert "not an image" in refused.value.detail

    # And the image itself comes back, with nothing allowed to run in it.
    served = await router.read_asset(project.id, stored.id, context=context, session=session)
    assert served.body == PNG
    assert served.headers["content-security-policy"].startswith("sandbox")

    await router.delete_asset(project.id, stored.id, context=context, session=session)
    assert await service.list_assets(session, project) == []
