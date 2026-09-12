"""The plan catalogue as it is right now, defaults plus any edits.

Read on the hot path — every run start asks what the limits are — so the merged
catalogue is cached in the process for a few seconds rather than fetched each
time. The staleness that buys is bounded and harmless: a price edit shows up
within the TTL, and no limit can change by more than one cache window.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.billing.models import UNLIMITED_SENTINEL, PlanOverride
from basivo_orch.billing.plans import PLAN_ORDER, PLANS, Plan
from basivo_orch.config import get_settings

CACHE_SECONDS = 30.0

_cache: dict[str, Plan] | None = None
_loaded_at: float = 0.0

#: The fields the console may change. Anything not here is not editable, which
#: is what keeps a plan code or an internal flag out of reach of a typo.
EDITABLE = (
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

_COUNTS = ("runs_per_month", "flows", "apps", "seats", "storage_mb")


def invalidate() -> None:
    """Drop the cache. Called after an edit so this process sees it at once."""
    global _cache, _loaded_at
    _cache = None
    _loaded_at = 0.0


def _merge(plan: Plan, row: PlanOverride) -> Plan:
    changes: dict[str, Any] = {}
    for field in EDITABLE:
        value = getattr(row, field)
        if value is None:
            continue
        if field in _COUNTS:
            changes[field] = None if value == UNLIMITED_SENTINEL else int(value)
        elif field == "features":
            changes[field] = tuple(str(item) for item in value)
        else:
            changes[field] = value
    return replace(plan, **changes) if changes else plan


async def catalogue(session: AsyncSession, *, fresh: bool = False) -> dict[str, Plan]:
    """Every plan, with the console's edits applied."""
    global _cache, _loaded_at

    if not fresh and _cache is not None and (time.monotonic() - _loaded_at) < CACHE_SECONDS:
        return _cache

    rows = (await session.execute(select(PlanOverride))).scalars().all()
    merged = dict(PLANS)
    for row in rows:
        if row.code in merged:
            merged[row.code] = _merge(merged[row.code], row)
        else:
            # A code nobody shipped: the row is the whole plan. Added from the
            # admin screen, so that a tier can be introduced without a deploy.
            merged[row.code] = _merge(_blank(row.code), row)

    _cache = merged
    _loaded_at = time.monotonic()
    return merged


def _blank(code: str) -> Plan:
    """The starting point for a plan that was created rather than shipped.

    Every limit is zero rather than unlimited, so a field the creator forgot
    is a plan that allows nothing and is noticed at once, rather than a plan
    that quietly hands out everything.
    """
    return Plan(
        code=code,
        name=code.replace("-", " ").title(),
        tagline="",
        price_inr="",
        price_usd="",
        runs_per_month=0,
        flows=0,
        apps=0,
        seats=1,
        history_days=7,
        storage_mb=0,
        features=(),
    )


def order(plans: dict[str, Plan]) -> list[str]:
    """The order plans are shown in: the ones we ship, then the ones added."""
    extras = sorted(code for code in plans if code not in PLAN_ORDER)
    return [code for code in PLAN_ORDER if code in plans] + extras


async def plan_of(session: AsyncSession, code: str | None) -> Plan:
    """One plan by code, falling back to Free when it is unknown."""
    plans = await catalogue(session)
    return plans.get(code or "free", plans["free"])


async def product_id(session: AsyncSession, code: str) -> str:
    """The provider product for a plan: the console's, else the environment's."""
    row = await session.get(PlanOverride, code)
    if row is not None and row.product_id:
        return row.product_id.strip()
    return get_settings().product_id(code)


async def set_override(
    session: AsyncSession,
    code: str,
    changes: dict[str, Any],
    *,
    user_id: uuid.UUID | None = None,
) -> Plan:
    """Apply an edit. A field set to None goes back to the built-in value."""
    row = await session.get(PlanOverride, code)
    if row is None:
        row = PlanOverride(code=code)
        session.add(row)

    for field, value in changes.items():
        if field in EDITABLE or field == "product_id":
            setattr(row, field, value)
    row.updated_by = user_id

    await session.commit()
    invalidate()
    return (await catalogue(session, fresh=True))[code]


async def clear_override(session: AsyncSession, code: str) -> Plan:
    """Put a plan back to the numbers it ships with."""
    row = await session.get(PlanOverride, code)
    if row is not None:
        await session.delete(row)
        await session.commit()
    invalidate()
    return (await catalogue(session, fresh=True))[code]


async def delete_plan(session: AsyncSession, code: str) -> None:
    """Remove a plan that was created here. Built-in plans cannot be removed.

    A workspace still on it keeps its subscription row and falls back to Free,
    which is the same place a lapsed one lands: nothing is deleted, and
    nobody is charged for a plan that no longer exists.
    """
    if code in PLANS:
        raise ValueError("That plan ships with the product and cannot be deleted.")
    row = await session.get(PlanOverride, code)
    if row is None:
        raise ValueError("There is no plan with that code.")
    await session.delete(row)
    await session.commit()
    invalidate()
