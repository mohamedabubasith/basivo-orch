"""Provider credentials.

The property that matters most here is not "does saving work" — it's "does the
secret ever come back out", and "can a workspace ever see another workspace's
key". Both are tested directly against the router's own functions, the same
way `test_analytics.py` tests tenant isolation without going through HTTP: the
authority check FastAPI would normally perform (`Depends(require(...))`) is
just a Python dependency, so calling the endpoint function with a constructed
`OrgContext` exercises the same code the real request path runs.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, Role
from basivo_orch.auth.models import Organization, User
from basivo_orch.credentials.crypto import DecryptionError, decrypt, encrypt
from basivo_orch.credentials.models import Credential
from basivo_orch.credentials.router import create_credential, delete_credential, list_credentials
from basivo_orch.credentials.schemas import CredentialCreate, CredentialRead


def make_context(organization: Organization, *, permissions: frozenset[Permission]) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106 — never verified; the gate runs after auth.
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=permissions
    )


ALL = frozenset(Permission)
FAKE_SECRET = "sk-live-secret-value"  # noqa: S105 — a test fixture value, not a real credential.


async def test_encrypt_decrypt_round_trips():
    token = encrypt(FAKE_SECRET)
    assert token != FAKE_SECRET
    assert decrypt(token) == FAKE_SECRET


async def test_tampered_ciphertext_fails_to_decrypt():
    token = encrypt("sk-live-secret-value")
    tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
    with pytest.raises(DecryptionError):
        decrypt(tampered)


async def test_the_response_schema_has_no_field_for_the_secret():
    """A regression guard, not a behavioural test: if someone ever adds
    `api_key` or `secret_encrypted` to `CredentialRead`, this fails loudly
    instead of the secret quietly starting to appear in `GET` responses."""
    assert "api_key" not in CredentialRead.model_fields
    assert "secret_encrypted" not in CredentialRead.model_fields


async def test_created_credential_stores_ciphertext_not_plaintext(
    session: AsyncSession, organization: Organization
):
    context = make_context(organization, permissions=ALL)
    payload = CredentialCreate(name="Prod Anthropic", provider="anthropic", api_key="sk-ant-abc123")

    created = await create_credential(payload, context=context, session=session)

    assert created.hint == "c123"
    assert created.provider == "anthropic"
    # The one property that matters: the plaintext key is nowhere on the row
    # that gets serialised back to the client.
    assert "sk-ant-abc123" not in created.secret_encrypted
    assert decrypt(created.secret_encrypted) == "sk-ant-abc123"


async def test_unknown_provider_is_rejected(session: AsyncSession, organization: Organization):
    context = make_context(organization, permissions=ALL)
    payload = CredentialCreate(name="Mystery", provider="not-a-real-provider", api_key="x")

    with pytest.raises(HTTPException) as raised:
        await create_credential(payload, context=context, session=session)
    assert raised.value.status_code == 422


async def test_duplicate_name_in_one_workspace_is_a_conflict(
    session: AsyncSession, organization: Organization
):
    context = make_context(organization, permissions=ALL)
    payload = CredentialCreate(name="Shared Name", provider="openai", api_key="sk-1")

    await create_credential(payload, context=context, session=session)
    with pytest.raises(HTTPException) as raised:
        await create_credential(payload, context=context, session=session)
    assert raised.value.status_code == 409


async def test_another_workspace_sees_none_of_it(session: AsyncSession, organization: Organization):
    other = Organization(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()
    await session.refresh(other)

    owner_ctx = make_context(organization, permissions=ALL)
    other_ctx = make_context(other, permissions=ALL)

    await create_credential(
        CredentialCreate(name="Owner's key", provider="anthropic", api_key="sk-owner"),
        context=owner_ctx,
        session=session,
    )

    owner_list = await list_credentials(context=owner_ctx, session=session)
    other_list = await list_credentials(context=other_ctx, session=session)

    assert len(owner_list) == 1
    assert other_list == []


async def test_deleting_another_workspaces_credential_404s(
    session: AsyncSession, organization: Organization
):
    other = Organization(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()
    await session.refresh(other)

    owner_ctx = make_context(organization, permissions=ALL)
    other_ctx = make_context(other, permissions=ALL)

    created = await create_credential(
        CredentialCreate(name="Owner's key", provider="anthropic", api_key="sk-owner"),
        context=owner_ctx,
        session=session,
    )

    with pytest.raises(HTTPException) as raised:
        await delete_credential(created.id, context=other_ctx, session=session)
    assert raised.value.status_code == 404

    # Untouched: the failed cross-tenant attempt did not delete it.
    still_there = await session.get(Credential, created.id)
    assert still_there is not None
