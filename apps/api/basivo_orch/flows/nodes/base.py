"""What a node is.

Every node — the Tier 1 utilities here and the Tier 2 capability nodes that
follow — implements this one interface. That is what lets the engine
instrument, retry and time all of them identically: a Code Agent node is a
larger implementation, not a different contract, so it gets the same run log
for free.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel

#: The port a node's output leaves by. Branching nodes name others.
DEFAULT_PORT = "out"

#: Alias for `type[BaseModel]`.
#:
#: Node subclasses declare a class attribute called `type` (the node's stable
#: identifier), which shadows the builtin inside the class body — so a bare
#: `ClassVar[type[BaseModel]]` annotation resolves to that string attribute
#: instead of the builtin and every use of `config_model` loses its type.
ConfigModel = type[BaseModel]


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """A stored provider credential, decrypted, for the duration of one call.

    Deliberately not the ORM row: a node has no business knowing the storage
    shape, only that it got a provider name, a secret, and whatever else that
    provider's SDK constructor wants.
    """

    provider: str
    api_key: str
    base_url: str | None
    options: dict[str, Any]


class NodeError(Exception):
    """A node failed in a way worth reporting to the flow's author.

    `retryable` decides whether the engine backs off and tries again. A 503
    from an upstream API is worth retrying; a malformed URL never will be, and
    retrying it three times just makes the run slower and the log noisier.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class NodeContext:
    """Everything a node is allowed to see."""

    run_id: uuid.UUID
    organization_id: uuid.UUID
    node_id: str
    node_name: str
    attempt: int

    #: Output of the upstream node, or the trigger payload for the first node.
    input: Any
    #: Every completed node's output, addressable as `nodes.<id>.output`.
    outputs: dict[str, Any]
    #: Values set by Set nodes, addressable as `vars.<name>`.
    variables: dict[str, Any]
    #: What started the run, addressable as `trigger.*`.
    trigger: dict[str, Any]

    #: Push a human-readable progress line to anyone streaming this run.
    #: Section 4's SSE contract has a `progress` field; this is what fills it.
    progress: Callable[[str], Awaitable[None]]

    #: Look up a stored credential by id, decrypted and scoped to this run's
    #: workspace. Returns `None` if it does not exist or belongs to another
    #: workspace — the two are indistinguishable on purpose, the same way a
    #: 404 and a cross-tenant read both just fail rather than leaking which.
    resolve_credential: Callable[[str], Awaitable[ResolvedCredential | None]]

    #: Push a *structured* step to the run's event log.
    #:
    #: `progress` carries a sentence for a human watching a stream. It is the
    #: wrong shape for what happens inside a capability node: an agent makes
    #: several model calls and several tool calls per execution, and "what did
    #: it actually do, what did each part cost" cannot be answered by parsing
    #: prose back out of a log line.
    #:
    #: Steps land in `run_event`, which is already append-only with a gapless
    #: per-run sequence — so the step log is durable, replayable and survives a
    #: dropped connection, for free. One node execution owns many steps.
    step: Callable[[str, dict[str, Any]], Awaitable[None]]

    http: httpx.AsyncClient

    #: Save bytes a node produced — a rendered poster, an export — and get back
    #: how to refer to them. Implemented by the engine, which owns the database
    #: session, so a node never writes SQL of its own.
    save_artifact: Callable[..., Awaitable[dict[str, Any]]] | None = None
    #: Read bytes another node saved, by id. None when it does not exist or
    #: belongs to another workspace.
    load_artifact: Callable[[str], Awaitable[bytes | None]] | None = None

    #: What this agent remembers about `subject` from previous runs, oldest
    #: turn first. Engine-provided for the same reason credentials are: the
    #: node never writes SQL.
    load_memory: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    #: Replace what it remembers. Called after a run with the windowed turns.
    save_memory: Callable[..., Awaitable[None]] | None = None

    def template_context(self) -> dict[str, Any]:
        return {
            "input": self.input,
            # Wrapped, so the addressable path is `nodes.<id>.output.*` — the
            # contract every docstring, the editor's autocomplete and the code
            # node's tests already promised. The engine exposed the raw value
            # at `nodes.<id>.*` instead, which meant every `.output` reference
            # the editor suggested failed at run time with "not available";
            # the integration suite caught it the first time a flow actually
            # used a cross-node reference. The envelope also reserves room for
            # `nodes.<id>.status` and friends without a breaking change.
            "nodes": {node_id: {"output": value} for node_id, value in self.outputs.items()},
            "vars": self.variables,
            "trigger": self.trigger,
            "run": {"id": str(self.run_id)},
        }


@dataclass
class NodeResult:
    """What a node produces."""

    output: Any = None
    #: Ports to continue from. `None` means the default port. A Condition node
    #: returns exactly one; a future fan-out node could return several.
    ports: list[str] | None = None
    #: Extra fields merged into the node's log row — token counts and cost from
    #: Tier 2 nodes, feeding the analysis layer's cost breakdown.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Variables to merge into the run's variable bag.
    variables: dict[str, Any] = field(default_factory=dict)


class Node(ABC):
    """Base class for every node type."""

    #: Stable identifier stored in the graph. Renaming one breaks saved flows.
    type: ClassVar[str]
    #: What the palette shows.
    label: ClassVar[str]
    description: ClassVar[str] = ""
    #: 1 = utility, 2 = capability. Drives grouping in the UI.
    tier: ClassVar[int] = 1
    category: ClassVar[str] = "utility"
    #: Trigger nodes start a flow and take no inputs.
    is_trigger: ClassVar[bool] = False
    #: Ports other than the default, for branching nodes.
    ports: ClassVar[tuple[str, ...]] = (DEFAULT_PORT,)
    #: Dotted paths into this node's output that are stable enough to suggest
    #: in the editor's template autocomplete — `("body", "usage.cost_usd")`
    #: becomes `{{ input.body }}` / `{{ nodes.<id>.output.usage.cost_usd }}`.
    #: Empty for nodes whose output shape is the author's own (code, manual
    #: trigger): suggesting made-up paths is worse than suggesting none.
    output_paths: ClassVar[tuple[str, ...]] = ()

    config_model: ClassVar[ConfigModel]

    #: Retry policy. Applied by the engine, not by each node, so behaviour is
    #: consistent and every attempt lands in the log.
    max_attempts: ClassVar[int] = 1
    retry_backoff_seconds: ClassVar[float] = 1.0
    timeout_seconds: ClassVar[float] = 60.0

    #: Whether running this node twice with the same input is harmless.
    #:
    #: Consulted when a run is recovered after the worker executing it died.
    #: Recovery re-runs the graph from the start, which is free for a node
    #: that only reads or computes — and wrong for one that already opened a
    #: pull request, filed an issue, or charged a card. Nodes that change the
    #: world outside this system set this to False, and a half-finished run
    #: that reached one is failed for a human rather than silently repeated.
    replay_safe: ClassVar[bool] = True

    @abstractmethod
    async def run(self, config: Any, ctx: NodeContext) -> NodeResult:
        """Do the work. Raise `NodeError` to fail with a readable message."""

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Palette metadata, so the UI is driven by the registry rather than a
        hand-maintained list that drifts from what the engine can actually run."""
        return {
            "type": cls.type,
            "label": cls.label,
            "description": cls.description,
            "tier": cls.tier,
            "category": cls.category,
            "is_trigger": cls.is_trigger,
            "ports": list(cls.ports),
            "output_paths": list(cls.output_paths),
            "config_schema": cls.config_model.model_json_schema(),
        }


def summarise(value: Any, *, limit: int = 2000) -> dict[str, Any] | None:
    """Shrink a payload to something safe to store on every node execution.

    Node inputs and outputs can be megabytes. Keeping them whole would make the
    log table the biggest thing in the database and slow every query the
    analysis layer wants to run, so what is stored is a shape plus a truncated
    preview — enough to debug, bounded in size.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        preview = {}
        for key in list(value)[:20]:
            item = value[key]
            preview[key] = summarise(item, limit=200) if isinstance(item, (dict, list)) else item
        return {
            "kind": "object",
            "keys": len(value),
            "preview": _truncate_strings(preview, limit=limit),
        }

    if isinstance(value, list):
        return {
            "kind": "array",
            "length": len(value),
            "preview": _truncate_strings(value[:5], limit=limit),
        }

    if isinstance(value, str):
        return {
            "kind": "string",
            "length": len(value),
            "preview": value[:limit] + ("…" if len(value) > limit else ""),
        }

    return {"kind": type(value).__name__, "value": value}


def _truncate_strings(value: Any, *, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit] + ("…" if len(value) > limit else "")
    if isinstance(value, dict):
        return {k: _truncate_strings(v, limit=limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(v, limit=limit) for v in value]
    return value
