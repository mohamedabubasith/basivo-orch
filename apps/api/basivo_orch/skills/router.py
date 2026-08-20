"""The skill library's HTTP surface.

Org-scoped through the same `require()` chokepoint as flows and credentials, so
a skill is exactly as isolated as a flow. Listing returns summaries rather than
bodies: a picker showing thirty skills should not transfer thirty procedures,
and the body is only interesting on the edit screen.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.authz import OrgContext, Permission, require
from basivo_orch.db import get_async_session
from basivo_orch.skills.models import Skill
from basivo_orch.skills.schemas import (
    SkillImport,
    SkillRead,
    SkillSummary,
    SkillWrite,
    parse_skill_md,
    to_skill_md,
)

router = APIRouter(tags=["skills"])


def _summary(skill: Skill) -> SkillSummary:
    return SkillSummary(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        load_count=skill.load_count,
        updated_at=skill.updated_at,
        resource_count=len(skill.resources or []),
        instruction_chars=len(skill.instructions or ""),
    )


async def _get(session: AsyncSession, organization_id: uuid.UUID, skill_id: uuid.UUID) -> Skill:
    result = await session.execute(
        select(Skill).where(Skill.id == skill_id, Skill.organization_id == organization_id)
    )
    if skill := result.scalar_one_or_none():
        return skill
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such skill.")


@router.get("/orgs/{organization_id}/skills", response_model=list[SkillSummary])
async def list_skills(
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> list[SkillSummary]:
    result = await session.execute(
        select(Skill).where(Skill.organization_id == context.organization_id).order_by(Skill.name)
    )
    return [_summary(skill) for skill in result.scalars()]


@router.get("/orgs/{organization_id}/skills/{skill_id}", response_model=SkillRead)
async def read_skill(
    skill_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> Skill:
    return await _get(session, context.organization_id, skill_id)


@router.get("/orgs/{organization_id}/skills/{skill_id}/export")
async def export_skill(
    skill_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """The skill as a `SKILL.md`, so it can live in a repository too."""
    skill = await _get(session, context.organization_id, skill_id)
    body = to_skill_md(skill.name, skill.description, skill.instructions)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{skill.name}.SKILL.md"'},
    )


@router.post(
    "/orgs/{organization_id}/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_skill(
    payload: SkillWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> Skill:
    skill = Skill(
        organization_id=context.organization_id,
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        resources=[resource.model_dump() for resource in payload.resources],
        created_by=context.user.id,
    )
    session.add(skill)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A skill called {payload.name!r} already exists in this workspace. The name is "
            "how an agent asks for it, so it has to be unique.",
        ) from None
    await session.refresh(skill)
    return skill


@router.post(
    "/orgs/{organization_id}/skills/import",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_skill(
    payload: SkillImport,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> Skill:
    """Create a skill from a `SKILL.md` file, frontmatter and all."""
    try:
        parsed = parse_skill_md(payload.content, fallback_name=payload.name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return await create_skill(parsed, context=context, session=session)


@router.put("/orgs/{organization_id}/skills/{skill_id}", response_model=SkillRead)
async def update_skill(
    skill_id: uuid.UUID,
    payload: SkillWrite,
    context: OrgContext = Depends(require(Permission.SKILL_WRITE)),
    session: AsyncSession = Depends(get_async_session),
) -> Skill:
    skill = await _get(session, context.organization_id, skill_id)
    skill.name = payload.name
    skill.description = payload.description
    skill.instructions = payload.instructions
    skill.resources = [resource.model_dump() for resource in payload.resources]
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Another skill is already called {payload.name!r}.",
        ) from None
    await session.refresh(skill)
    return skill


@router.delete("/orgs/{organization_id}/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: uuid.UUID,
    context: OrgContext = Depends(require(Permission.SKILL_DELETE)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a skill.

    Flows reference skills by id and are not rewritten here. A flow whose skill
    has been deleted still runs — the agent is simply not offered it, and the
    run log says so (`skill.missing`) rather than failing silently. Failing the
    run instead would take a whole workflow down over a library edit; saying
    nothing would leave someone debugging an agent that quietly got worse.
    """
    skill = await _get(session, context.organization_id, skill_id)
    await session.delete(skill)
    await session.commit()


@router.get("/orgs/{organization_id}/skills/stats/summary")
async def skill_stats(
    context: OrgContext = Depends(require(Permission.SKILL_READ)),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, int]:
    result = await session.execute(
        select(func.count(), func.coalesce(func.sum(Skill.load_count), 0)).where(
            Skill.organization_id == context.organization_id
        )
    )
    count, loads = result.one()
    return {"skills": int(count), "loads": int(loads)}
