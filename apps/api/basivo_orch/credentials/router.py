"""Credential management.

Every route is org-scoped through the same `require()` chokepoint the flow
routes use, so a credential is exactly as isolated as a flow: created inside a
workspace, visible only to its members, and gone the moment membership is.

The secret never appears in a response. `POST` accepts it once; every other
route — including the one this router doesn't have, `GET /{id}` with the
secret — simply does not exist.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.credentials.crypto import decrypt, encrypt
from basivo_orch.credentials.model_catalog import (
    ModelFetchFailed,
    ModelFetchNotSupported,
    fetch_models,
)
from basivo_orch.credentials.models import Credential
from basivo_orch.credentials.schemas import (
    PROVIDERS,
    CredentialCreate,
    CredentialRead,
    CredentialTestRequest,
    ModelListResponse,
)
from basivo_orch.db import get_async_session

router = APIRouter(tags=["credentials"])


@router.get("/orgs/{organization_id}/credentials", response_model=list[CredentialRead])
async def list_credentials(
    context: OrgContext = Depends(require(Permission.CREDENTIAL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[Credential]:
    result = await session.execute(
        select(Credential)
        .where(Credential.organization_id == context.organization_id)
        .order_by(Credential.name)
    )
    return list(result.scalars())


@router.post(
    "/orgs/{organization_id}/credentials",
    response_model=CredentialRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    payload: CredentialCreate,
    context: OrgContext = Depends(require(Permission.CREDENTIAL_CREATE)),
    session: AsyncSession = Depends(get_async_session),
) -> Credential:
    if payload.provider not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown provider {payload.provider!r}. Choose one of: {', '.join(PROVIDERS)}.",
        )

    record = Credential(
        organization_id=context.organization_id,
        name=payload.name,
        provider=payload.provider,
        secret_encrypted=encrypt(payload.api_key),
        hint=payload.api_key[-4:] if len(payload.api_key) >= 4 else "",
        base_url=payload.base_url,
        options=payload.options,
        created_by=context.user.id,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A credential with that name already exists in this workspace.",
        ) from None
    await session.commit()
    await session.refresh(record)
    return record


@router.delete(
    "/orgs/{organization_id}/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_credential(
    credential_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.CREDENTIAL_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    result = await session.execute(
        select(Credential).where(
            Credential.id == credential_id,
            Credential.organization_id == context.organization_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such credential.")
    await session.delete(record)
    await session.commit()


@router.get("/orgs/{organization_id}/credentials/providers")
async def list_providers(
    context: OrgContext = Depends(require(Permission.CREDENTIAL_READ)),
) -> list[str]:
    """Every provider the Agent node can authenticate against."""
    return PROVIDERS


@router.post("/orgs/{organization_id}/credentials/test", response_model=ModelListResponse)
async def test_credential(
    payload: CredentialTestRequest,
    context: OrgContext = Depends(require(Permission.CREDENTIAL_CREATE)),
) -> ModelListResponse:
    """Does this key actually work? Fired before anything is saved.

    Fetching a provider's model catalog needs exactly what running an agent
    against it needs — a working key — so the two questions share one call:
    if the list comes back, the key is good. Requires create permission, not
    just read, since it makes a real outbound request using a candidate
    secret the caller just typed.
    """
    if payload.provider not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown provider {payload.provider!r}. Choose one of: {', '.join(PROVIDERS)}.",
        )
    try:
        models = await fetch_models(
            payload.provider,
            api_key=payload.api_key,
            base_url=payload.base_url or "",
            options=payload.options,
        )
        return ModelListResponse(supported=True, models=models)
    except ModelFetchNotSupported:
        return ModelListResponse(supported=False)
    except ModelFetchFailed as exc:
        return ModelListResponse(supported=True, error=str(exc))


@router.get(
    "/orgs/{organization_id}/credentials/{credential_id}/models",
    response_model=ModelListResponse,
)
async def credential_models(
    credential_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.CREDENTIAL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> ModelListResponse:
    """The live model list for an already-saved credential.

    Read permission is enough here — unlike `test`, this never sees a secret
    the caller typed; it decrypts one that was already stored, the same trust
    level as running an Agent node that references it.
    """
    result = await session.execute(
        select(Credential).where(
            Credential.id == credential_id,
            Credential.organization_id == context.organization_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such credential.")

    try:
        models = await fetch_models(
            record.provider,
            api_key=decrypt(record.secret_encrypted),
            base_url=record.base_url or "",
            options=record.options,
        )
        return ModelListResponse(supported=True, models=models)
    except ModelFetchNotSupported:
        return ModelListResponse(supported=False)
    except ModelFetchFailed as exc:
        return ModelListResponse(supported=True, error=str(exc))
