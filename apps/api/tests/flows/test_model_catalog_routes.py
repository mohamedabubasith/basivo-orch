"""The two routes built on `model_catalog`: pre-save "Test connection" and a
saved credential's live model list.

`fetch_models` itself is not exercised here — that would mean a real network
call to a real provider with a real key, which this suite does not have and
should not depend on. What is tested is that these routes translate its three
outcomes (a model list, "not supported", "failed") into the right response
shape, and that they hold the same permission and tenant-isolation properties
as the rest of the credentials router.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.credentials.crypto import encrypt
from basivo_orch.credentials.model_catalog import ModelFetchFailed, ModelFetchNotSupported
from basivo_orch.credentials.models import Credential

# Aliased: pytest would otherwise collect the imported route function as a
# test, because its real name starts with `test_`.
from basivo_orch.credentials.router import credential_models
from basivo_orch.credentials.router import test_credential as connection_test_route
from basivo_orch.credentials.schemas import CredentialTestRequest


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def test_test_connection_reports_the_live_model_list(monkeypatch, organization):
    async def fake_fetch(provider, *, api_key, base_url, options):
        assert provider == "anthropic"
        assert api_key == "sk-candidate"
        return ["claude-sonnet-5", "claude-opus-5"]

    monkeypatch.setattr("basivo_orch.credentials.router.fetch_models", fake_fetch)

    result = await connection_test_route(
        CredentialTestRequest(provider="anthropic", api_key="sk-candidate"),
        context=make_context(organization),
    )
    assert result.supported is True
    assert result.error is None
    assert result.models == ["claude-sonnet-5", "claude-opus-5"]


async def test_test_connection_reports_a_bad_key_without_raising(monkeypatch, organization):
    async def fake_fetch(provider, *, api_key, base_url, options):
        raise ModelFetchFailed("401 Unauthorized")

    monkeypatch.setattr("basivo_orch.credentials.router.fetch_models", fake_fetch)

    result = await connection_test_route(
        CredentialTestRequest(provider="anthropic", api_key="sk-wrong"),
        context=make_context(organization),
    )
    # A 200 with a reported failure, not a 4xx/5xx — this endpoint's entire
    # job is to answer "does it work", and "no" is a normal answer.
    assert result.supported is True
    assert result.error == "401 Unauthorized"
    assert result.models == []


async def test_test_connection_reports_unsupported_providers_honestly(monkeypatch, organization):
    async def fake_fetch(provider, *, api_key, base_url, options):
        raise ModelFetchNotSupported(provider)

    monkeypatch.setattr("basivo_orch.credentials.router.fetch_models", fake_fetch)

    result = await connection_test_route(
        CredentialTestRequest(provider="bedrock", api_key="anything"),
        context=make_context(organization),
    )
    assert result.supported is False
    assert result.error is None
    assert result.models == []


async def test_test_connection_rejects_an_unknown_provider(organization):
    with pytest.raises(HTTPException) as raised:
        await connection_test_route(
            CredentialTestRequest(provider="not-a-provider", api_key="x"),
            context=make_context(organization),
        )
    assert raised.value.status_code == 422


async def test_credential_models_uses_the_stored_decrypted_key(monkeypatch, session, organization):
    record = Credential(
        organization_id=organization.id,
        name="Prod key",
        provider="anthropic",
        secret_encrypted=encrypt("sk-stored-secret"),
        hint="cret",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    async def fake_fetch(provider, *, api_key, base_url, options):
        assert api_key == "sk-stored-secret"
        return ["claude-sonnet-5"]

    monkeypatch.setattr("basivo_orch.credentials.router.fetch_models", fake_fetch)

    result = await credential_models(record.id, context=make_context(organization), session=session)
    assert result.supported is True
    assert result.models == ["claude-sonnet-5"]


async def test_credential_models_404s_for_another_workspaces_credential(
    monkeypatch, session, organization
):
    record = Credential(
        organization_id=organization.id,
        name="Prod key",
        provider="anthropic",
        secret_encrypted=encrypt("sk-stored-secret"),
        hint="cret",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    other = Organization(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()
    await session.refresh(other)

    with pytest.raises(HTTPException) as raised:
        await credential_models(record.id, context=make_context(other), session=session)
    assert raised.value.status_code == 404


async def test_a_subscription_token_has_no_catalog_and_is_not_an_error() -> None:
    """`claude setup-token` output only signs in Claude Code. Asking Anthropic's
    model list with it would fail; saying "no catalog" keeps the save clean."""
    from basivo_orch.credentials.model_catalog import fetch_models

    with pytest.raises(ModelFetchNotSupported):
        await fetch_models("anthropic", api_key="sk-ant-oat01-token", base_url="", options={})
