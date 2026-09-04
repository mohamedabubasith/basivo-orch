"""The Repo picker's data: which repositories a code credential can open."""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.credentials.crypto import encrypt
from basivo_orch.credentials.models import Credential
from basivo_orch.credentials.repo_catalog import (
    RepoFetchFailed,
    RepoFetchNotSupported,
    fetch_repos,
)
from basivo_orch.credentials.router import credential_repos


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_github_repos_come_back_as_owner_name_newest_first() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=[{"full_name": "acme/website"}, {"full_name": "acme/api"}])

    repos = await fetch_repos("github", api_key="ghp_x", base_url="", http=client(handler))
    assert repos == ["acme/website", "acme/api"]
    assert seen["url"].startswith("https://api.github.com/user/repos?")
    assert "sort=updated" in seen["url"] and seen["auth"] == "Bearer ghp_x"


async def test_gitlab_uses_the_project_path_and_private_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["private-token"] == "glpat-x"
        assert str(request.url).startswith("https://gitlab.example/api/v4/projects?")
        return httpx.Response(200, json=[{"path_with_namespace": "group/project"}])

    repos = await fetch_repos(
        "gitlab", api_key="glpat-x", base_url="https://gitlab.example", http=client(handler)
    )
    assert repos == ["group/project"]


async def test_a_rejected_token_is_a_readable_failure_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(RepoFetchFailed, match="401"):
        await fetch_repos("github", api_key="bad", base_url="", http=client(lambda r: handler(r)))


async def test_a_model_credential_has_no_repositories() -> None:
    with pytest.raises(RepoFetchNotSupported):
        await fetch_repos("openai", api_key="sk", base_url="")


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def test_the_route_decrypts_the_saved_token_and_lists_repos(
    monkeypatch, organization, session
) -> None:
    record = Credential(
        organization_id=organization.id,
        name="gh",
        provider="github",
        secret_encrypted=encrypt("ghp_saved"),
        base_url=None,
        options={},
    )
    session.add(record)
    await session.commit()

    async def fake_fetch(provider, *, api_key, base_url):
        assert provider == "github" and api_key == "ghp_saved"
        return ["acme/website"]

    monkeypatch.setattr("basivo_orch.credentials.router.fetch_repos", fake_fetch)
    result = await credential_repos(record.id, context=make_context(organization), session=session)
    assert result.supported is True and result.repos == ["acme/website"]
