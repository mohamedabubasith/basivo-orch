"""The analysis layer.

These assert the numbers, because a dashboard that is confidently wrong is
worse than no dashboard: people act on it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from basivo_orch.flows.analytics import analytics, error_signature
from basivo_orch.flows.models import NodeExecution, NodeStatus, Run, RunStatus, TriggerKind


async def make_run(session, flow, version, org, *, status=RunStatus.SUCCEEDED, duration=1000):
    run = Run(
        flow_id=flow.id,
        flow_version_id=version.id,
        organization_id=org.id,
        trigger=TriggerKind.MANUAL,
        input={},
        status=status,
        duration_ms=duration,
        created_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


def execution(run, node_id, *, status, attempt=1, duration=100, error=None, node_type="data.set"):
    return NodeExecution(
        run_id=run.id,
        node_id=node_id,
        node_type=node_type,
        node_name=node_id,
        status=status,
        attempt=attempt,
        duration_ms=duration,
        error=error,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
async def flow_and_version(session, organization):
    from basivo_orch.flows.models import Flow, FlowVersion

    flow = Flow(organization_id=organization.id, name="F", slug=f"f-{uuid.uuid4().hex[:8]}")
    session.add(flow)
    await session.flush()
    version = FlowVersion(flow_id=flow.id, version=1, graph={"nodes": [], "edges": []})
    session.add(version)
    await session.flush()
    return flow, version


# ---------------------------------------------------------------------------
# Error clustering
# ---------------------------------------------------------------------------


def test_the_same_failure_with_different_numbers_is_one_cluster() -> None:
    """Forty failed runs are usually two causes. A reader shown forty problems
    fixes none of them."""
    a = error_signature("connection refused to 10.0.0.4:5432 after 3021ms")
    b = error_signature("connection refused to 10.0.0.9:5432 after 87ms")
    assert a == b


def test_ids_and_urls_do_not_split_a_cluster() -> None:
    a = error_signature("run 3f2504e0-4f89-11d3-9a0c-0305e82c3301 failed calling https://a.test/x")
    b = error_signature("run 7c9e6679-7425-40de-944b-e07fc1f90ae7 failed calling https://b.test/y")
    assert a == b


def test_genuinely_different_failures_stay_apart() -> None:
    assert error_signature("connection refused") != error_signature("certificate expired")


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------


async def test_latency_attribution_sums_to_the_whole(session, organization, flow_and_version):
    """'The flow is slow' is not actionable. 'One node is 80% of it' is."""
    flow, version = flow_and_version
    run = await make_run(session, flow, version, organization)
    session.add_all(
        [
            execution(run, "fast", status=NodeStatus.SUCCEEDED, duration=100),
            execution(run, "slow", status=NodeStatus.SUCCEEDED, duration=900),
        ]
    )
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    shares = {n["node_id"]: n["share_of_runtime"] for n in report["nodes"]}
    assert shares["slow"] == pytest.approx(0.9)
    assert shares["fast"] == pytest.approx(0.1)
    assert sum(shares.values()) == pytest.approx(1.0)
    # Ordered worst-first, so the hotspot is the first thing read.
    assert report["nodes"][0]["node_id"] == "slow"


@pytest.mark.security
async def test_a_node_rescued_by_a_retry_is_counted(session, organization, flow_and_version):
    """The metric other dashboards cannot show.

    This node's final state is SUCCEEDED, so every tool that counts final
    states reports it as perfectly healthy. It failed first and only worked on
    the retry, which is the earliest visible sign of an upstream service
    degrading — and it is only visible because each attempt is its own row.
    """
    flow, version = flow_and_version
    run = await make_run(session, flow, version, organization)
    session.add_all(
        [
            execution(run, "flaky", status=NodeStatus.FAILED, attempt=1, error="503"),
            execution(run, "flaky", status=NodeStatus.SUCCEEDED, attempt=2),
        ]
    )
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    assert report["retry_rescued_runs"] == 1
    flaky = next(n for n in report["nodes"] if n["node_id"] == "flaky")
    assert flaky["retry_rescued"] == 1
    # And the run still reads as a success, which is exactly why the plain
    # success rate hides this.
    assert report["runs"]["success_rate"] == 1.0


async def test_a_first_time_success_is_not_a_rescue(session, organization, flow_and_version):
    flow, version = flow_and_version
    run = await make_run(session, flow, version, organization)
    session.add(execution(run, "clean", status=NodeStatus.SUCCEEDED, attempt=1))
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    assert report["retry_rescued_runs"] == 0


async def test_a_branch_that_never_fires_is_reported(session, organization, flow_and_version):
    """Dead logic, or a path no test has ever exercised. Neither shows up in a
    success rate, and both are worth knowing."""
    flow, version = flow_and_version
    for _ in range(3):
        run = await make_run(session, flow, version, organization)
        session.add_all(
            [
                execution(run, "taken", status=NodeStatus.SUCCEEDED),
                execution(run, "never", status=NodeStatus.SKIPPED, duration=0),
            ]
        )
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    dead = {d["node_id"] for d in report["dead_branches"]}
    assert dead == {"never"}


async def test_a_failure_rate_is_withheld_on_a_tiny_sample(session, organization, flow_and_version):
    """One failure in two runs is not a 50% failure rate worth acting on."""
    flow, version = flow_and_version
    for status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED):
        run = await make_run(session, flow, version, organization)
        session.add(
            execution(run, "n", status=status, error="x" if status is NodeStatus.FAILED else None)
        )
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    assert next(n for n in report["nodes"] if n["node_id"] == "n")["failure_rate"] is None


async def test_a_failure_rate_appears_once_the_sample_is_real(
    session, organization, flow_and_version
):
    flow, version = flow_and_version
    for i in range(10):
        run = await make_run(session, flow, version, organization)
        failed = i < 3
        session.add(
            execution(
                run,
                "n",
                status=NodeStatus.FAILED if failed else NodeStatus.SUCCEEDED,
                error="boom" if failed else None,
            )
        )
    await session.commit()

    report = await analytics(session, organization_id=organization.id)
    assert next(n for n in report["nodes"] if n["node_id"] == "n")["failure_rate"] == pytest.approx(
        0.3
    )


async def test_another_tenant_sees_none_of_it(session, organization, flow_and_version):
    """The whole dashboard is one query shape; a missing tenant filter would
    leak every customer's operational data at once."""
    from basivo_orch.auth.models import Organization

    flow, version = flow_and_version
    run = await make_run(session, flow, version, organization)
    session.add(execution(run, "a", status=NodeStatus.SUCCEEDED))

    other = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    session.add(other)
    await session.commit()

    report = await analytics(session, organization_id=other.id)
    assert report["runs"]["total"] == 0
    assert report["nodes"] == []
