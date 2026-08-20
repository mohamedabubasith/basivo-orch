"""The workflow engine.

Executes a graph node by node and records what happened. The recording is not a
side effect here — section 3 of the SOW makes the run log a product feature, so
the engine's job is equally "run the flow" and "leave behind a log good enough
to analyse".

Execution model: waves over the graph with an *active set*. Every node whose
predecessors have all settled runs in the same wave, concurrently — two agents
hanging off one trigger are independent work, and running them one after the
other made a 59s branch and a 3m49s branch take 4m48s instead of 3m49s. A node
runs when it is reachable through an edge that actually fired. Condition nodes
fire one port, so the branch not taken is marked SKIPPED rather than left
absent — an absent row is indistinguishable from a node that never existed,
and would quietly corrupt the per-node reliability figures the analysis layer
computes.

Concurrency has one hard constraint: the whole run shares a single
`AsyncSession`, and SQLAlchemy sessions are not safe under concurrent use. So
node *bodies* run in parallel while every database touch — node rows, event
writes, credential reads — is serialised behind `self._db`. Model calls and
HTTP are where the wall-clock actually goes; the database work is microseconds.
"""

from __future__ import annotations

import asyncio
import uuid
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

#: How many nodes may be in flight at once. Parallelism is the point, but a
#: fan-out of thirty agents firing thirty simultaneous model calls earns a
#: provider rate-limit instead of a fast run.
MAX_PARALLEL_NODES = 8


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
        #: Serialises every use of `session`, shared with the event writer so a
        #: node committing its row cannot interleave with an event write.
        self._db = asyncio.Lock()
        self.events = EventWriter(session, run.id, redis_client, lock=self._db)
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

        async with self._db:
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

    #: A poster is a few hundred kilobytes; a minute of 1080p video is tens of
    #: megabytes. This ceiling is what keeps "files live in Postgres" an honest
    #: simplification rather than a trap — and the number that will eventually
    #: argue for object storage, when someone stores something longer.
    MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

    async def _save_artifact(
        self,
        data: bytes,
        *,
        filename: str = "file",
        content_type: str = "application/octet-stream",
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """Store bytes a node produced and return how to refer to them."""
        from basivo_orch.flows.models import Artifact

        if len(data) > self.MAX_ARTIFACT_BYTES:
            raise NodeError(
                f"That file is {len(data) // 1024}KB and the limit is "
                f"{self.MAX_ARTIFACT_BYTES // 1024}KB. Render it smaller, or at a lower scale."
            )

        artifact = Artifact(
            organization_id=self.run.organization_id,
            run_id=self.run.id,
            node_id=node_id,
            filename=filename[:200],
            content_type=content_type[:120],
            size_bytes=len(data),
            data=data,
        )
        async with self._db:
            self.session.add(artifact)
            await self.session.commit()
            await self.session.refresh(artifact)

        return {
            "artifact_id": str(artifact.id),
            "filename": artifact.filename,
            "content_type": artifact.content_type,
            "size_bytes": artifact.size_bytes,
            # Relative on purpose: the public base URL is deployment
            # configuration, and a stored absolute URL is wrong the moment the
            # deployment moves.
            "url": f"/api/v1/orgs/{self.run.organization_id}/artifacts/{artifact.id}",
        }

    async def _load_artifact(self, artifact_id: str) -> bytes | None:
        """Read bytes another node saved. Scoped to this run's workspace."""
        import uuid as _uuid

        from basivo_orch.flows.models import Artifact

        try:
            key = _uuid.UUID(str(artifact_id))
        except ValueError:
            return None
        async with self._db:
            artifact = await self.session.get(Artifact, key)
        if artifact is None or artifact.organization_id != self.run.organization_id:
            return None
        return artifact.data

    async def _memory_scope(self, node_id: str) -> str:
        """Which agent owns a memory. Flow plus node, never the run.

        Keyed on the flow rather than the flow *version*, or every publish
        would amnesia the agent — and on the node id so two agents in one flow
        keep separate memories, which is what "this agent remembers" means.
        """
        return f"{self.run.flow_id}:{node_id}"

    async def _load_memory(self, node_id: str, subject: str) -> list[dict[str, Any]]:
        from sqlalchemy import select as _select

        from basivo_orch.flows.models import AgentMemory

        async with self._db:
            result = await self.session.execute(
                _select(AgentMemory).where(
                    AgentMemory.organization_id == self.run.organization_id,
                    AgentMemory.scope == await self._memory_scope(node_id),
                    AgentMemory.subject == subject[:300],
                )
            )
            row = result.scalar_one_or_none()
        return list(row.turns or []) if row else []

    async def _save_memory(self, node_id: str, subject: str, turns: list[dict[str, Any]]) -> None:
        from sqlalchemy import select as _select

        from basivo_orch.flows.models import AgentMemory

        scope = await self._memory_scope(node_id)
        async with self._db:
            result = await self.session.execute(
                _select(AgentMemory).where(
                    AgentMemory.organization_id == self.run.organization_id,
                    AgentMemory.scope == scope,
                    AgentMemory.subject == subject[:300],
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AgentMemory(
                    organization_id=self.run.organization_id,
                    scope=scope,
                    subject=subject[:300],
                )
                self.session.add(row)
            row.turns = turns
            row.updated_at = datetime.now(UTC)
            await self.session.commit()

    async def _load_skills(self, skill_ids: list[str]) -> list[Any]:
        """Selected skills, tenant-scoped, in the order the node lists them.

        Missing ids are skipped rather than raising: a flow whose skill was
        deleted from the library keeps running, and the node logs which ones
        vanished. Failing the run would let a library edit take down every
        workflow that referenced it.
        """
        from sqlalchemy import select as _select

        from basivo_orch.flows.nodes.skills import LoadedSkill
        from basivo_orch.skills.models import Skill

        wanted: list[uuid.UUID] = []
        for value in skill_ids:
            try:
                wanted.append(uuid.UUID(str(value)))
            except (ValueError, AttributeError, TypeError):
                continue
        if not wanted:
            return []

        async with self._db:
            result = await self.session.execute(
                _select(Skill).where(
                    Skill.id.in_(wanted),
                    Skill.organization_id == self.run.organization_id,
                )
            )
            found = {row.id: row for row in result.scalars()}

        return [
            LoadedSkill(
                id=str(found[key].id),
                name=found[key].name,
                description=found[key].description,
                instructions=found[key].instructions,
                resources=list(found[key].resources or []),
            )
            for key in wanted
            if key in found
        ]

    async def _record_skill_load(self, skill_id: str) -> None:
        """Bump the usage counter. Best effort — a run must not fail over it."""
        from sqlalchemy import update as _update

        from basivo_orch.skills.models import Skill

        try:
            async with self._db:
                await self.session.execute(
                    _update(Skill)
                    .where(Skill.id == uuid.UUID(str(skill_id)))
                    # An expression, not a read-modify-write: several nodes can
                    # load the same skill in one wave, and Python-side
                    # increments would lose all but the last.
                    .values(load_count=Skill.load_count + 1)
                )
                await self.session.commit()
        except Exception:  # pragma: no cover - telemetry, never load-bearing
            log.warning("skill.load_count_failed", skill_id=str(skill_id))

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
            async with self._db:
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
        async with self._db:
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
        """Execute the graph in waves, everything independent running together.

        A node joins a wave once every predecessor has *settled* (run or been
        skipped), which is exactly when the sequential pass would have reached
        it — same ordering guarantees, same skip semantics, minus the waiting.
        `topological_order` still runs first: it is what raises on a cycle, and
        its order breaks ties inside a wave so logs read top-to-bottom.
        """
        order = topological_order(self.graph)
        trigger = next(node for node in self.graph.nodes if node_registry.get(node.type).is_trigger)
        rank = {node_id: index for index, node_id in enumerate(order)}
        pending = {node_id for node_id in order if self.graph.node(node_id) is not None}
        settled: set[str] = set()
        gate = asyncio.Semaphore(MAX_PARALLEL_NODES)

        while pending:
            wave = sorted(
                (
                    node_id
                    for node_id in pending
                    if all(edge.source in settled for edge in self.graph.incoming(node_id))
                ),
                key=lambda node_id: rank[node_id],
            )
            if not wave:
                # Unreachable: topological_order already rejects cycles. Raising
                # beats spinning forever if that ever stops being true.
                raise NodeError(f"Deadlocked with {len(pending)} node(s) unreachable.")

            results = await asyncio.gather(
                *(self._settle(node_id, trigger.id, http, gate) for node_id in wave),
                # A sibling's failure must not orphan the branches already in
                # flight: they finish, record their rows, and the run fails on
                # the first error afterwards. Cancelling mid-model-call would
                # bill the tokens and log nothing.
                return_exceptions=True,
            )
            pending -= set(wave)
            settled |= set(wave)

            for node_id, outcome in zip(wave, results, strict=True):
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome is None:  # skipped
                    continue
                self.outputs[node_id] = outcome.output
                self.variables |= outcome.variables
                fired = outcome.ports or [DEFAULT_PORT]
                for edge in self.graph.outgoing(node_id):
                    port = edge.source_handle or DEFAULT_PORT
                    if port in fired:
                        self._arrivals[edge.target].add(port)

    async def _settle(
        self, node_id: str, trigger_id: str, http: httpx.AsyncClient, gate: asyncio.Semaphore
    ) -> NodeResult | None:
        """Run one node, or record it as skipped. Returns None when skipped."""
        node = self.graph.node(node_id)
        assert node is not None  # pending only ever holds real nodes
        incoming = self.graph.incoming(node_id)
        if node_id != trigger_id and not self._is_active(node_id, incoming):
            await self._record_skipped(node)
            return None
        async with gate:
            return await self._run_one(node, self._input_for(node_id, incoming), http)

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
        async with self._db:
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
            async with self._db:
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
                save_artifact=self._save_artifact,
                load_artifact=self._load_artifact,
                load_memory=self._load_memory,
                save_memory=self._save_memory,
                load_skills=self._load_skills,
                record_skill_load=self._record_skill_load,
                http=http,
            )

            try:
                async with asyncio.timeout(implementation.timeout_seconds):
                    result = await implementation.run(config, ctx)

                finished = datetime.now(UTC)
                async with self._db:
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
                async with self._db:
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
