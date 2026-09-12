"""The platform admin API.

Not a second console: a handful of read-only numbers plus the one thing that
has to be changeable without a deploy, which is what a plan costs. Every route
is behind `require_platform_admin`, and a caller who is not staff is told the
path does not exist.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.admin import stats
from basivo_orch.admin.deps import require_platform_admin
from basivo_orch.auth.models import User
from basivo_orch.billing.models import UNLIMITED_SENTINEL, PlanOverride
from basivo_orch.billing.plans import PLANS
from basivo_orch.billing.pricing import (
    catalogue,
    clear_override,
    delete_plan,
    order,
    set_override,
)
from basivo_orch.billing.schemas import PlanRead
from basivo_orch.config import get_settings
from basivo_orch.db import get_async_session

router = APIRouter(prefix="/admin", tags=["admin"])


class PlanAdminRead(PlanRead):
    """A plan, plus where its numbers come from."""

    #: Which fields were edited from the console rather than shipped with it.
    overridden: list[str]
    #: The provider product a customer is sent to, if one is set here.
    product_id: str


class PlanUpdate(BaseModel):
    """An edit. A field left out is not touched; a field set to null goes back
    to the value the product ships with."""

    name: str | None = Field(default=None, max_length=64)
    tagline: str | None = Field(default=None, max_length=200)
    price_inr: str | None = Field(default=None, max_length=32)
    price_usd: str | None = Field(default=None, max_length=32)
    runs_per_month: int | None = None
    flows: int | None = None
    apps: int | None = None
    seats: int | None = None
    history_days: int | None = Field(default=None, ge=1, le=3650)
    storage_mb: int | None = None
    features: list[str] | None = None
    product_id: str | None = Field(default=None, max_length=128)
    #: Explicitly clear these fields, since "absent" already means "leave it".
    reset: list[str] = Field(default_factory=list)

    @field_validator("runs_per_month", "flows", "apps", "seats", "storage_mb")
    @classmethod
    def _sane_limit(cls, value: int | None) -> int | None:
        """`-1` is how the API says unlimited. Anything else below zero is a
        typo, and a plan with minus five runs would refuse every request."""
        if value is None:
            return None
        if value == UNLIMITED_SENTINEL or value >= 0:
            return value
        raise ValueError("A limit is a whole number, or -1 for no limit.")

    @field_validator("features")
    @classmethod
    def _short_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 12:
            raise ValueError("A plan lists at most 12 features.")
        return [item.strip() for item in value if item.strip()]


class PlanCreate(BaseModel):
    """A plan that did not ship with the product.

    Every number is required. A tier added with half its limits missing is a
    tier that sells something nobody defined, and the person adding it is the
    one who knows what it should be.
    """

    model_config = {"extra": "forbid"}

    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=64)
    tagline: str = Field(default="", max_length=200)
    price_inr: str = Field(default="", max_length=32)
    price_usd: str = Field(default="", max_length=32)
    runs_per_month: int = Field(ge=-1)
    flows: int = Field(ge=-1)
    apps: int = Field(ge=-1)
    seats: int = Field(ge=-1)
    history_days: int = Field(ge=1, le=3_650)
    storage_mb: int = Field(ge=-1)
    features: list[str] = Field(default_factory=list)
    #: The provider product a customer is sent to. Without it the plan can be
    #: shown and assigned but not bought, which the console says on the card.
    product_id: str = Field(default="", max_length=128)


def _describe(session_plan, row: PlanOverride | None) -> PlanAdminRead:
    base = PlanRead.of(session_plan).model_dump()
    overridden = (
        [
            field
            for field in (
                "name",
                "tagline",
                "price_inr",
                "price_usd",
                "runs_per_month",
                "flows",
                "apps",
                "seats",
                "history_days",
                "storage_mb",
                "features",
            )
            if getattr(row, field, None) is not None
        ]
        if row is not None
        else []
    )
    product = (row.product_id if row and row.product_id else "") or get_settings().product_id(
        session_plan.code
    )
    return PlanAdminRead(**base, overridden=overridden, product_id=product)


@router.get("/plans", response_model=list[PlanAdminRead])
async def list_plans(
    _: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> list[PlanAdminRead]:
    plans = await catalogue(session, fresh=True)
    return [_describe(plans[code], await session.get(PlanOverride, code)) for code in order(plans)]


@router.patch("/plans/{code}", response_model=PlanAdminRead)
async def edit_plan(
    code: str,
    payload: PlanUpdate,
    admin: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> PlanAdminRead:
    """Change what a plan says and what it allows.

    The price here is what the console SHOWS. What a card is actually charged
    lives with the payment provider, so a price change means making the new
    product there and putting its id in `product_id` in the same edit.
    """
    if code not in await catalogue(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no plan called {code!r}.")

    changes: dict[str, Any] = payload.model_dump(exclude_unset=True, exclude={"reset"})
    for field in payload.reset:
        changes[field] = None

    plan = await set_override(session, code, changes, user_id=admin.id)
    return _describe(plan, await session.get(PlanOverride, code))


@router.delete("/plans/{code}", response_model=PlanAdminRead | None)
async def reset_plan(
    code: str,
    _: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> PlanAdminRead | None:
    """Forget what the console stored for this plan.

    One route, because it is one act: the stored row goes. For a plan that
    ships with the product that means its numbers go back to the built-in
    ones, and the plan is returned. For a plan somebody added here there is
    nothing underneath, so the plan itself is gone and the answer is empty. A
    workspace still on it falls back to Free, the same place a lapsed
    subscription lands, and nothing it owns is deleted.
    """
    if code not in await catalogue(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no plan called {code!r}.")
    if code not in PLANS:
        await delete_plan(session, code)
        return None
    plan = await clear_override(session, code)
    return _describe(plan, None)


@router.get("/overview")
async def read_overview(
    days: int = Query(default=7, ge=1, le=90),
    _: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Workspaces, people, runs and the slowest node types."""
    return await stats.overview(session, days=days)


@router.get("/errors")
async def read_errors(
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    kind: Literal["grouped", "recent"] = "grouped",
    _: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """What is failing. Grouped by message, or the latest failures as they are."""
    if kind == "recent":
        return {"kind": kind, "items": await stats.recent_failures(session, days=days, limit=limit)}
    return {"kind": kind, "items": await stats.top_errors(session, days=days, limit=limit)}


@router.get("/workspaces")
async def read_workspaces(
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Who is using the product, busiest first."""
    return {"items": await stats.workspaces(session, days=days, limit=limit)}


@router.post("/plans", response_model=PlanAdminRead, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate,
    admin: User = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_async_session),
) -> PlanAdminRead:
    """Add a tier that did not ship with the product.

    The plan appears on the billing page at once. What a card is charged still
    lives with the payment provider, so a new plan needs its product made there
    and its id given here, or the upgrade button has nowhere to send anybody.
    """
    if payload.code in await catalogue(session):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"There is already a plan called {payload.code!r}."
        )

    changes = payload.model_dump(exclude={"code"})
    plan = await set_override(session, payload.code, changes, user_id=admin.id)
    return _describe(plan, await session.get(PlanOverride, payload.code))
