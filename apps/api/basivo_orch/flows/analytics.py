"""What the run log is for.

Section 3 of the SOW asks for failure clustering, latency and cost hotspots, and
suggestions — not a list of executions with green ticks. Every metric here is
chosen because it answers a question an operator actually has and most
orchestrators cannot answer at all:

* **Where does the time go?** Per-node share of total runtime. "The flow is
  slow" is not actionable; "78% of it is one HTTP call" is.
* **What is being rescued by retries?** A node that succeeds only on its second
  attempt looks perfectly healthy in every dashboard that counts final states.
  It is the earliest visible sign of an upstream service degrading, and it is
  invisible unless attempts are recorded separately — which is why
  `node_execution` stores one row per attempt rather than one per node.
* **How many *distinct* problems are there?** Forty failed runs are usually two
  causes. Clustering by error shape turns a wall into a to-do list.
* **Which branches never run?** A Condition whose other side has never been
  taken is either dead logic or a test that has never been exercised. Both are
  worth knowing and neither shows up in a success rate.

Everything is scoped to one organisation and computed in SQL where the counting
belongs, with percentiles taken over a bounded sample in Python — see
`_percentile`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows.models import NodeExecution, NodeStatus, Run, RunStatus

#: Ceiling on rows pulled for percentile work. Percentiles need the individual
#: values, and `percentile_cont` is Postgres-only while the test suite runs on
#: SQLite. A capped sample keeps one code path and bounds the memory; the cap is
#: reported in the response so a reader knows when they are seeing a sample.
DURATION_SAMPLE_LIMIT = 5000

#: Below this many executions a rate is noise. Three failures out of four runs
#: is not a 75% failure rate worth acting on, and presenting it as one sends
#: people chasing ghosts.
MIN_EXECUTIONS_FOR_RATE = 5


def _percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile. Returns None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


#: Strips the parts of an error message that differ between otherwise identical
#: failures, so they cluster. Order matters: UUIDs before the generic number
#: rule, or the number rule chews them up first.
_NOISE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<id>"),
    (re.compile(r"https?://[^\s'\"]+"), "<url>"),
    # No word boundaries. `\b\d+\b` never matches the number in "87ms" — digit
    # and letter are both word characters, so there is no boundary between them
    # — and durations glued to their unit are exactly what varies between two
    # occurrences of the same failure.
    (re.compile(r"\d+\.\d+"), "<n>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"'[^']{40,}'"), "'<text>'"),
    (re.compile(r"\s+"), " "),
]


def error_signature(message: str) -> str:
    """Collapse an error to the shape it shares with its siblings.

    "connection refused to 10.0.0.4:5432 after 3021ms" and the same message
    with different numbers are one problem, and a reader who sees them as forty
    problems will not fix any of them.
    """
    signature = message.strip()
    for pattern, replacement in _NOISE:
        signature = pattern.sub(replacement, signature)
    return signature[:200]


def _window(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _scoped(
    statement: Select[Any], organization_id: uuid.UUID, flow_id: uuid.UUID | None
) -> Select[Any]:
    statement = statement.where(Run.organization_id == organization_id)
    if flow_id is not None:
        statement = statement.where(Run.flow_id == flow_id)
    return statement


async def analytics(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    flow_id: uuid.UUID | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Everything the dashboard shows, in one round of queries."""
    since = _window(days)

    # ---- runs ------------------------------------------------------------
    run_rows = await session.execute(
        _scoped(
            select(Run.status, func.count(), func.sum(Run.duration_ms)).where(
                Run.created_at >= since
            ),
            organization_id,
            flow_id,
        ).group_by(Run.status)
    )
    by_status: dict[str, int] = {}
    total_runs = 0
    for status, count, _total_ms in run_rows:
        by_status[str(status)] = count
        total_runs += count

    duration_rows = await session.execute(
        _scoped(
            select(Run.duration_ms).where(
                Run.created_at >= since,
                Run.duration_ms.isnot(None),
                Run.status == RunStatus.SUCCEEDED,
            ),
            organization_id,
            flow_id,
        ).limit(DURATION_SAMPLE_LIMIT)
    )
    run_durations = [row[0] for row in duration_rows]

    succeeded = by_status.get(RunStatus.SUCCEEDED.value, 0)
    failed = by_status.get(RunStatus.FAILED.value, 0)

    # ---- node executions -------------------------------------------------
    node_rows = await session.execute(
        _scoped(
            select(
                NodeExecution.node_id,
                NodeExecution.node_type,
                func.max(NodeExecution.node_name),
                NodeExecution.status,
                func.count(),
                func.sum(NodeExecution.duration_ms),
            )
            .join(Run, Run.id == NodeExecution.run_id)
            .where(Run.created_at >= since),
            organization_id,
            flow_id,
        ).group_by(NodeExecution.node_id, NodeExecution.node_type, NodeExecution.status)
    )

    nodes: dict[str, dict[str, Any]] = {}
    for node_id, node_type, node_name, status, count, total_ms in node_rows:
        entry = nodes.setdefault(
            node_id,
            {
                "node_id": node_id,
                "node_type": node_type,
                "node_name": node_name or node_id,
                "executions": 0,
                "succeeded": 0,
                "failed": 0,
                "skipped": 0,
                "total_ms": 0,
            },
        )
        key = str(status)
        entry["executions"] += count
        entry[key] = entry.get(key, 0) + count
        entry["total_ms"] += int(total_ms or 0)

    # Per-node durations, for percentiles.
    sample_rows = await session.execute(
        _scoped(
            select(NodeExecution.node_id, NodeExecution.duration_ms)
            .join(Run, Run.id == NodeExecution.run_id)
            .where(
                Run.created_at >= since,
                NodeExecution.duration_ms.isnot(None),
                NodeExecution.status == NodeStatus.SUCCEEDED,
            ),
            organization_id,
            flow_id,
        ).limit(DURATION_SAMPLE_LIMIT)
    )
    samples: dict[str, list[int]] = {}
    for node_id, duration in sample_rows:
        samples.setdefault(node_id, []).append(duration)

    # Retry-rescued: a node that failed at least once in a run and still ended
    # up succeeding. Counted per (run, node) so one flaky node in one run is one
    # rescue, not one per attempt.
    rescued_rows = await session.execute(
        _scoped(
            select(NodeExecution.run_id, NodeExecution.node_id)
            .join(Run, Run.id == NodeExecution.run_id)
            .where(Run.created_at >= since)
            .group_by(NodeExecution.run_id, NodeExecution.node_id)
            .having(func.max(NodeExecution.attempt) > 1)
            .having(func.sum(case((NodeExecution.status == NodeStatus.SUCCEEDED, 1), else_=0)) > 0),
            organization_id,
            flow_id,
        )
    )
    rescued_pairs = list(rescued_rows)
    rescued_by_node: dict[str, int] = {}
    for _run_id, node_id in rescued_pairs:
        rescued_by_node[node_id] = rescued_by_node.get(node_id, 0) + 1

    total_node_ms = sum(entry["total_ms"] for entry in nodes.values()) or 1
    node_list = []
    for entry in nodes.values():
        durations = samples.get(entry["node_id"], [])
        ran = entry["succeeded"] + entry["failed"]
        node_list.append(
            {
                **{k: entry[k] for k in ("node_id", "node_type", "node_name", "executions")},
                "succeeded": entry["succeeded"],
                "failed": entry["failed"],
                "skipped": entry["skipped"],
                "total_ms": entry["total_ms"],
                "share_of_runtime": round(entry["total_ms"] / total_node_ms, 4),
                "p50_ms": _percentile(durations, 0.50),
                "p95_ms": _percentile(durations, 0.95),
                # Suppressed below a floor rather than shown as a wild number
                # from three samples.
                "failure_rate": (
                    round(entry["failed"] / ran, 4) if ran >= MIN_EXECUTIONS_FOR_RATE else None
                ),
                "retry_rescued": rescued_by_node.get(entry["node_id"], 0),
            }
        )
    node_list.sort(key=lambda n: n["total_ms"], reverse=True)

    # ---- failure clusters -------------------------------------------------
    failure_rows = await session.execute(
        _scoped(
            select(
                NodeExecution.error,
                NodeExecution.node_type,
                func.max(NodeExecution.node_name),
                func.max(NodeExecution.started_at),
            )
            .join(Run, Run.id == NodeExecution.run_id)
            .where(
                Run.created_at >= since,
                NodeExecution.status == NodeStatus.FAILED,
                NodeExecution.error.isnot(None),
            )
            .group_by(NodeExecution.error, NodeExecution.node_type),
            organization_id,
            flow_id,
        ).limit(DURATION_SAMPLE_LIMIT)
    )

    clusters: dict[str, dict[str, Any]] = {}
    for message, node_type, node_name, last_seen in failure_rows:
        signature = error_signature(message)
        cluster = clusters.setdefault(
            signature,
            {
                "signature": signature,
                "example": message[:300],
                "node_type": node_type,
                "node_name": node_name,
                "count": 0,
                "last_seen": last_seen,
            },
        )
        cluster["count"] += 1
        if last_seen and (cluster["last_seen"] is None or last_seen > cluster["last_seen"]):
            cluster["last_seen"] = last_seen
    cluster_list = sorted(clusters.values(), key=lambda c: c["count"], reverse=True)[:10]

    # ---- branches that never fire -----------------------------------------
    dead = [
        {
            "node_id": n["node_id"],
            "node_name": n["node_name"],
            "node_type": n["node_type"],
            "skipped": n["skipped"],
        }
        for n in node_list
        if n["skipped"] > 0 and n["succeeded"] == 0 and n["failed"] == 0
    ]

    return {
        "window_days": days,
        "generated_at": datetime.now(UTC),
        "sampled": len(run_durations) >= DURATION_SAMPLE_LIMIT,
        "runs": {
            "total": total_runs,
            "succeeded": succeeded,
            "failed": failed,
            "running": by_status.get(RunStatus.RUNNING.value, 0)
            + by_status.get(RunStatus.QUEUED.value, 0),
            "success_rate": round(succeeded / total_runs, 4) if total_runs else None,
            "p50_ms": _percentile(run_durations, 0.50),
            "p95_ms": _percentile(run_durations, 0.95),
        },
        # The headline number nobody else shows: how many runs completed only
        # because something was retried. Zero is healthy; a rising number is an
        # upstream service degrading before it starts failing outright.
        "retry_rescued_runs": len({run_id for run_id, _ in rescued_pairs}),
        "nodes": node_list,
        "failure_clusters": cluster_list,
        "dead_branches": dead,
    }
