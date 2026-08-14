"""The workflow engine.

Executes a graph node by node and records what happened. The recording is not a
side effect here — section 3 of the SOW makes the run log a product feature, so
the engine's job is equally "run the flow" and "leave behind a log good enough
to analyse".

Execution model: a single topological pass with an *active set*. A node runs
when it is reachable through an edge that actually fired. Condition nodes fire
one port, so the branch not taken is marked SKIPPED rather than left absent —
an absent row is indistinguishable from a node that never existed, and would
quietly corrupt the per-node reliability figures the analysis layer computes.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from basivo_orch.flows import nodes as node_registry
from basivo_orch.flows.events import EventWriter, RedisClient
from basivo_orch.flows.graph import Graph, topological_order
from basivo_orch.flows.models import NodeExecution, NodeStatus, Run, RunStatus
from basivo_orch.flows.nodes.base import (
    DEFAULT_PORT,
    NodeContext,
    NodeError,
    NodeResult,
    ResolvedCredential,
    summarise,
)
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: Ceiling on a whole run. Without one, a flow whose HTTP node points at a
#: server that accepts the connection and never answers holds a worker forever.
RUN_TIMEOUT_SECONDS = 900


class RunCancelled(Exception):
    """Raised when a run is cancelled while executing."""


class Engine:
    """Executes one run. Not reusable — construct one per run."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        run: Run,
        graph: Graph,
        redis_client: RedisClient | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.session = session
        self.run = run
        self.graph = graph
        self.events = EventWriter(session, run.id, redis_client)
        self._http = http
        self._owns_http = http is None

        self.outputs: dict[str, Any] = {}
        self.variables: dict[str, Any] = {}
        #: node_id -> set of ports that delivered into it.
        self._arrivals: dict[str, set[str]] = defaultdict(set)

    async def _resolve_credential(self, credential_id: str) -> ResolvedCredential | None:
        """Decrypt one credential, scoped to this run's workspace.

        Lives on the engine, not the node, because the engine is the one thing
        that holds a database session — keeping SQL out of node code is what
        lets a node's config be validated and displayed without ever touching
        the database.
        """
        import uuid as _uuid

        from basivo_orch.credentials.crypto import decrypt
        from basivo_orch.credentials.models import Credential

        try:
            credential_uuid = _uuid.UUID(credential_id)
        except ValueError:
            return None

        record = await self.session.get(Credential, credential_uuid)
        if record is None or record.organization_id != self.run.organization_id:
            return None

        record.last_used_at = datetime.now(UTC)
        await self.session.commit()

        return ResolvedCredential(
            provider=record.provider,
            api_key=decrypt(record.secret_encrypted),
            base_url=record.base_url,
            options=record.options,
        )

    async def execute(self) -> Run:
        http = self._http or httpx.AsyncClient(
            # Flows call third-party endpoints; the pool is shared across the
            # run's nodes so a ten-node flow does not open ten connections.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            headers={"User-Agent": "Basivo-Orchestrator/0.1"},
        )
        started = datetime.now(UTC)

        try:
            self.run.status = RunStatus.RUNNING
            self.run.started_at = started
            await self.session.commit()
            await self.events.emit("run.started", {"run_id": str(self.run.id)})

            try:
                async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                    await self._run_nodes(http)
            except TimeoutError:
                raise NodeError(
                    f"The run exceeded the {RUN_TIMEOUT_SECONDS}s limit and was stopped."
                ) from None

            self.run.status = RunStatus.SUCCEEDED
            self.run.output = self._final_output()
            await self._finish(started)
            await self.events.emit(
                "run.succeeded", {"run_id": str(self.run.id), "output": self.run.output}
            )

        except Exception as exc:
            self.run.status = RunStatus.FAILED
            self.run.error = str(exc)[:2000]
            await self._finish(started)
            await self.events.emit(
                "run.failed", {"run_id": str(self.run.id), "error": self.run.error}
            )
            log.warning(
                "run.failed", run_id=str(self.run.id), flow_id=str(self.run.flow_id), error=str(exc)
            )
        finally:
            if self._owns_http:
                await http.aclose()

        return self.run

    async def _finish(self, started: datetime) -> None:
        finished = datetime.now(UTC)
        self.run.finished_at = finished
        self.run.duration_ms = int((finished - started).total_seconds() * 1000)
        await self.session.commit()

    def _final_output(self) -> dict[str, Any]:
        """What the run returns to its caller.

        The outputs of every node with no outgoing edge that actually ran. A
        flow that branches has one live terminal; taking "the last node in
        topological order" instead would return whichever leaf happened to sort
        last, including one on a branch that was skipped.
        """
        terminals = [
            node.id
            for node in self.graph.nodes
            if node.id in self.outputs and not self.graph.outgoing(node.id)
        ]
        if len(terminals) == 1:
            return {"result": self.outputs[terminals[0]]}
        return {"result": {node_id: self.outputs[node_id] for node_id in terminals}}

    async def _run_nodes(self, http: httpx.AsyncClient) -> None:
        order = topological_order(self.graph)
        trigger = next(node for node in self.graph.nodes if node_registry.get(node.type).is_trigger)

        for node_id in order:
            node = self.graph.node(node_id)
            if node is None:
                continue

            incoming = self.graph.incoming(node_id)
            if node_id != trigger.id and not self._is_active(node_id, incoming):
                await self._record_skipped(node)
                continue

            upstream = self._input_for(node_id, incoming)
            result = await self._run_one(node, upstream, http)

            self.outputs[node_id] = result.output
            self.variables |= result.variables

            fired = result.ports or [DEFAULT_PORT]
            for edge in self.graph.outgoing(node_id):
                port = edge.source_handle or DEFAULT_PORT
                if port in fired:
                    self._arrivals[edge.target].add(port)

    def _is_active(self, node_id: str, incoming: list[Any]) -> bool:
        # Any live incoming edge is enough. A join node downstream of a branch
        # should run when either side reaches it, not only when both do — the
        # alternative deadlocks every diamond in every graph.
        return bool(self._arrivals.get(node_id))

    def _input_for(self, node_id: str, incoming: list[Any]) -> Any:
        """What this node receives.

        A single upstream passes its output straight through. Several pass a
        dict keyed by source id, so a join can address each branch explicitly
        rather than depending on which arrived last.
        """
        sources = [e.source for e in incoming if e.source in self.outputs]
        if not sources:
            return self.run.input
        if len(sources) == 1:
            return self.outputs[sources[0]]
        return {source: self.outputs[source] for source in sources}

    async def _record_skipped(self, node: Any) -> None:
        self.session.add(
            NodeExecution(
                run_id=self.run.id,
                node_id=node.id,
                node_type=node.type,
                node_name=node.name,
                status=NodeStatus.SKIPPED,
                attempt=1,
                duration_ms=0,
                finished_at=datetime.now(UTC),
            )
        )
        await self.session.commit()
        await self.events.emit(
            "node.skipped",
            {
                "node": node.name or node.id,
                "node_id": node.id,
                "node_type": node.type,
                "status": "skipped",
            },
        )

    async def _run_one(self, node: Any, upstream: Any, http: httpx.AsyncClient) -> NodeResult:
        """Run a node, with retries, recording every attempt."""
        implementation = node_registry.get(node.type)
        config = implementation.config_model.model_validate(node.config)
        last_error: Exception | None = None

        for attempt in range(1, implementation.max_attempts + 1):
            started = datetime.now(UTC)
            record = NodeExecution(
                run_id=self.run.id,
                node_id=node.id,
                node_type=node.type,
                node_name=node.name,
                status=NodeStatus.RUNNING,
                attempt=attempt,
                input_summary=summarise(upstream),
                started_at=started,
            )
            self.session.add(record)
            await self.session.commit()

            await self.events.emit(
                "node.started",
                {
                    "node": node.name or node.id,
                    "node_id": node.id,
                    "node_type": node.type,
                    "status": "running",
                    "attempt": attempt,
                },
            )

            async def progress(message: str, _node: Any = node) -> None:
                await self.events.emit(
                    "node.progress",
                    {
                        "node": _node.name or _node.id,
                        "node_id": _node.id,
                        "node_type": _node.type,
                        "status": "running",
                        "progress": message,
                    },
                )

            async def step(
                kind: str, data: dict[str, Any], _node: Any = node, _attempt: int = attempt
            ) -> None:
                """One structured step inside a node execution.

                Emitted as its own event rather than folded into the node's row:
                an agent performs several model calls and several tool calls per
                execution, and a single row cannot say which tool ran, what it
                cost, or in what order. `run_event` already gives these a
                gapless order and replay for free.
                """
                await self.events.emit(
                    "node.step",
                    {
                        "node": _node.name or _node.id,
                        "node_id": _node.id,
                        "node_type": _node.type,
                        "attempt": _attempt,
                        "step": kind,
                        **data,
                    },
                )

            ctx = NodeContext(
                run_id=self.run.id,
                organization_id=self.run.organization_id,
                node_id=node.id,
                node_name=node.name or node.id,
                attempt=attempt,
                input=upstream,
                outputs=self.outputs,
                variables=self.variables,
                trigger=self.run.input or {},
                progress=progress,
                step=step,
                resolve_credential=self._resolve_credential,
                http=http,
            )

            try:
                async with asyncio.timeout(implementation.timeout_seconds):
                    result = await implementation.run(config, ctx)

                finished = datetime.now(UTC)
                record.status = NodeStatus.SUCCEEDED
                record.output_summary = summarise(result.output)
                record.finished_at = finished
                record.duration_ms = int((finished - started).total_seconds() * 1000)
                for key in ("cost_usd", "tokens_in", "tokens_out"):
                    if key in result.metrics:
                        setattr(record, key, result.metrics[key])
                await self.session.commit()

                await self.events.emit(
                    "node.succeeded",
                    {
                        "node": node.name or node.id,
                        "node_id": node.id,
                        "node_type": node.type,
                        "status": "succeeded",
                        "duration_ms": record.duration_ms,
                        "ports": result.ports or [DEFAULT_PORT],
                    },
                )
                return result

            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    exc = NodeError(
                        f"{node.type} exceeded its {implementation.timeout_seconds}s limit.",
                        retryable=True,
                    )

                finished = datetime.now(UTC)
                record.status = NodeStatus.FAILED
                record.error = str(exc)[:2000]
                record.finished_at = finished
                record.duration_ms = int((finished - started).total_seconds() * 1000)
                await self.session.commit()
                last_error = exc

                retryable = isinstance(exc, NodeError) and exc.retryable
                final = attempt >= implementation.max_attempts or not retryable

                await self.events.emit(
                    "node.failed",
                    {
                        "node": node.name or node.id,
                        "node_id": node.id,
                        "node_type": node.type,
                        "status": "failed",
                        "error": record.error,
                        "attempt": attempt,
                        "will_retry": not final,
                    },
                )

                if final:
                    break

                # Exponential backoff. A node that failed because an upstream
                # service is struggling should not be retried immediately —
                # that is how a retry policy turns a blip into an outage.
                delay = implementation.retry_backoff_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        raise NodeError(
            f"Node {node.name or node.id!r} ({node.type}) failed: {last_error}"
        ) from last_error
