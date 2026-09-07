"""Product-wide numbers, for deciding what to fix next.

Read-only and deliberately coarse. Nothing here is per-customer support
tooling: it answers "what is failing across the product", "who is using it",
and "which node types are slow", which are the questions that change a backlog.

Payloads are never included. A failing run's input is the customer's data, and
a product dashboard is not a place to keep it; the error text and the node type
are what a fix is written from.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.auth.models import Membership, Organization, User
from basivo_orch.billing.models import Subscription, SubscriptionStatus
from basivo_orch.flows.models import Flow, NodeExecution, NodeStatus, Run, RunStatus

#: How many failures are read before grouping. A cap, because this is a
#: dashboard and not an export: the shape of the top errors does not change
#: between two thousand samples and a million.
SAMPLE_LIMIT = 2_000

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUMBER = re.compile(r"\b\d{2,}\b")
_QUOTED = re.compile(r"'[^']{8,}'")


def signature(message: str) -> str:
    """One error, with the parts that differ per occurrence taken out.

    Without this, a thousand copies of the same failure look like a thousand
    different problems, because each carries its own id.
    """
    text = " ".join((message or "").strip().split())
    text = _UUID.sub("<id>", text)
    text = _QUOTED.sub("'<value>'", text)
    text = _NUMBER.sub("<n>", text)
    return text[:180] or "no message"


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, days))


async def _count(session: AsyncSession, statement) -> int:
    return int((await session.execute(statement)).scalar_one())


async def overview(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    since = _since(days)

    runs_by_status: dict[str, int] = {}
    rows = await session.execute(
        select(Run.status, func.count()).where(Run.created_at >= since).group_by(Run.status)
    )
    for status_value, count in rows:
        key = status_value.value if hasattr(status_value, "value") else str(status_value)
        runs_by_status[key] = int(count)

    total_runs = sum(runs_by_status.values())
    failed = runs_by_status.get(RunStatus.FAILED.value, 0)

    plan_counts: dict[str, int] = {}
    subscription_rows = await session.execute(
        select(Subscription.plan, Subscription.status, func.count()).group_by(
            Subscription.plan, Subscription.status
        )
    )
    paying = 0
    for plan, status_value, count in subscription_rows:
        key = f"{plan}:{status_value}"
        plan_counts[key] = int(count)
        if status_value == SubscriptionStatus.ACTIVE:
            paying += int(count)

    organizations = await _count(session, select(func.count()).select_from(Organization))
    # Everyone without a subscription row is on the free plan.
    subscribed = await _count(session, select(func.count()).select_from(Subscription))

    slow = []
    slow_rows = await session.execute(
        select(
            NodeExecution.node_type,
            func.count(),
            func.avg(NodeExecution.duration_ms),
        )
        .where(NodeExecution.started_at >= since, NodeExecution.duration_ms.is_not(None))
        .group_by(NodeExecution.node_type)
        .order_by(func.avg(NodeExecution.duration_ms).desc())
        .limit(8)
    )
    for node_type, count, average in slow_rows:
        slow.append(
            {
                "node_type": node_type,
                "runs": int(count),
                "average_ms": int(average or 0),
            }
        )

    return {
        "window_days": days,
        "workspaces": {
            "total": organizations,
            "new": await _count(
                session,
                select(func.count())
                .select_from(Organization)
                .where(Organization.created_at >= since),
            ),
            "free": max(organizations - subscribed, 0),
            "paying": paying,
            "by_plan": plan_counts,
        },
        "people": {
            "total": await _count(session, select(func.count()).select_from(User)),
            "new": await _count(
                session, select(func.count()).select_from(User).where(User.created_at >= since)
            ),
            "unverified": await _count(
                session,
                select(func.count()).select_from(User).where(User.is_verified.is_(False)),
            ),
        },
        "flows": {"total": await _count(session, select(func.count()).select_from(Flow))},
        "runs": {
            "total": total_runs,
            "by_status": runs_by_status,
            "failure_rate": round(failed / total_runs, 4) if total_runs else 0.0,
            "queued_now": await _count(
                session,
                select(func.count()).select_from(Run).where(Run.status == RunStatus.QUEUED),
            ),
        },
        "slowest_nodes": slow,
    }


async def top_errors(
    session: AsyncSession, *, days: int = 7, limit: int = 20
) -> list[dict[str, Any]]:
    """The failures worth fixing, most common first."""
    since = _since(days)
    rows = (
        await session.execute(
            select(
                NodeExecution.node_type,
                NodeExecution.error,
                NodeExecution.run_id,
                NodeExecution.started_at,
            )
            .where(
                NodeExecution.status == NodeStatus.FAILED,
                NodeExecution.started_at >= since,
                NodeExecution.error.is_not(None),
            )
            .order_by(NodeExecution.started_at.desc())
            .limit(SAMPLE_LIMIT)
        )
    ).all()

    counter: Counter[tuple[str, str]] = Counter()
    sample: dict[tuple[str, str], dict[str, Any]] = {}
    for node_type, error, run_id, started_at in rows:
        key = (node_type, signature(error or ""))
        counter[key] += 1
        sample.setdefault(
            key,
            {
                "run_id": str(run_id),
                "last_seen": started_at.isoformat() if started_at else None,
                "message": " ".join((error or "").split())[:400],
            },
        )

    return [
        {
            "node_type": node_type,
            "signature": text,
            "count": count,
            **sample[(node_type, text)],
        }
        for (node_type, text), count in counter.most_common(limit)
    ]


async def recent_failures(
    session: AsyncSession, *, days: int = 7, limit: int = 25
) -> list[dict[str, Any]]:
    """The latest failed runs, so a fresh breakage is visible immediately."""
    since = _since(days)
    rows = (
        await session.execute(
            select(Run, Flow.name, Organization.name)
            .join(Flow, Flow.id == Run.flow_id)
            .join(Organization, Organization.id == Run.organization_id)
            .where(Run.status == RunStatus.FAILED, Run.created_at >= since)
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "run_id": str(run.id),
            "flow": flow_name,
            "workspace": org_name,
            "organization_id": str(run.organization_id),
            "error": " ".join((run.error or "").split())[:400],
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "duration_ms": run.duration_ms,
        }
        for run, flow_name, org_name in rows
    ]


async def workspaces(
    session: AsyncSession, *, days: int = 30, limit: int = 50
) -> list[dict[str, Any]]:
    """Who is actually using the product, busiest first."""
    since = _since(days)

    run_counts = dict(
        (
            await session.execute(
                select(Run.organization_id, func.count())
                .where(Run.created_at >= since)
                .group_by(Run.organization_id)
            )
        ).all()
    )
    seat_counts = dict(
        (
            await session.execute(
                select(Membership.organization_id, func.count()).group_by(
                    Membership.organization_id
                )
            )
        ).all()
    )
    plans: dict[uuid.UUID, tuple[str, str]] = {
        row.organization_id: (row.plan, row.status)
        for row in (await session.execute(select(Subscription))).scalars()
    }

    rows = (await session.execute(select(Organization).limit(500))).scalars().all()
    listed = [
        {
            "organization_id": str(org.id),
            "name": org.name,
            "slug": org.slug,
            "is_active": org.is_active,
            "created_at": org.created_at.isoformat() if org.created_at else None,
            "members": int(seat_counts.get(org.id, 0)),
            "runs": int(run_counts.get(org.id, 0)),
            "plan": plans.get(org.id, ("free", "active"))[0],
            "status": plans.get(org.id, ("free", "active"))[1],
        }
        for org in rows
    ]
    listed.sort(key=lambda item: item["runs"], reverse=True)
    return listed[:limit]
