"""The graph a flow is made of, and the rules it must satisfy.

Validation runs at save time, not at run time. A graph that cannot execute
should be rejected while the author is looking at it, not three days later when
a webhook fires at 3am — which is also the only way the run log stays a record
of real failures rather than of typos.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field, field_validator

#: Kept as literals rather than imported from the node modules: this validator
#: runs on every save, and importing the node package pulls in langchain.
HANDOVER_PORT = "handover"
AGENT_TYPE = "agent.llm"

MAX_NODES = 200
MAX_EDGES = 500


class GraphNode(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    type: str = Field(min_length=1, max_length=80)
    name: str | None = Field(default=None, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)
    #: Canvas coordinates. Carried through untouched; the engine ignores them.
    position: dict[str, float] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        # Node ids appear in templates as `nodes.<id>.output`, so keep them to
        # characters a dotted path can address unambiguously.
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Node ids may contain only letters, digits, '-' and '_'.")
        return value


class GraphEdge(BaseModel):
    source: str
    target: str
    #: Which output port of the source this leaves from. Condition nodes emit
    #: "true"/"false"; everything else uses the default port.
    source_handle: str | None = None


class Graph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    def node(self, node_id: str) -> GraphNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def outgoing(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self.edges if e.source == node_id]

    def incoming(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self.edges if e.target == node_id]


class GraphError(Exception):
    """A graph that cannot be executed. Carries every problem, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _who(node: GraphNode | None, fallback: str = "") -> str:
    """How a problem refers to a node: by the name the author gave it.

    The editor always saves a name (it defaults to the node's label), so a
    person reads "Render Poster: Html is required", not a generated id. Graphs
    built by hand fall back to the id, which is then the only name there is.
    """
    if node is None:
        return fallback
    return node.name or node.id


def _config_problems(node: GraphNode, spec: Any, exc: Exception) -> list[str]:
    """Pydantic's report, in the words of the form the author is looking at."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [f"{_who(node)}: {exc}"]
    fields = getattr(spec.config_model, "model_fields", {})
    out: list[str] = []
    for error in errors():
        loc = [str(part) for part in error.get("loc", ())]
        key = loc[0] if loc else ""
        field = fields.get(key)
        title = (
            field.title if field is not None and field.title else key.replace("_", " ").capitalize()
        ) or "Configuration"
        msg = str(error.get("msg", "invalid"))
        if msg == "Field required":
            msg = "is required"
        elif msg.startswith("Input should be "):
            msg = "should be " + msg[len("Input should be ") :]
        else:
            msg = msg[0].lower() + msg[1:] if msg else msg
        out.append(f"{_who(node)}: {title} {msg}.")
    return out or [f"{_who(node)}: the configuration is not valid."]


def validate_graph(graph: Graph, *, known_types: dict[str, Any]) -> None:
    """Reject anything the engine could not run.

    Collects all problems rather than raising on the first, so the editor can
    underline every broken node in one pass instead of making the author fix
    them one save at a time.
    """
    problems: list[str] = []

    if not graph.nodes:
        raise GraphError(["A flow needs at least one node."])
    if len(graph.nodes) > MAX_NODES:
        problems.append(f"A flow may have at most {MAX_NODES} nodes.")
    if len(graph.edges) > MAX_EDGES:
        problems.append(f"A flow may have at most {MAX_EDGES} edges.")

    ids = [n.id for n in graph.nodes]
    duplicates = {i for i in ids if ids.count(i) > 1}
    for dup in sorted(duplicates):
        problems.append(f"Duplicate node id {dup!r}.")
    id_set = set(ids)

    # --- node types and their configuration --------------------------------
    triggers: list[GraphNode] = []
    for node in graph.nodes:
        spec = known_types.get(node.type)
        if spec is None:
            problems.append(f"{_who(node)} has unknown type {node.type!r}.")
            continue
        if spec.is_trigger:
            triggers.append(node)
        try:
            spec.config_model.model_validate(node.config)
        except Exception as exc:  # pydantic ValidationError, kept readable
            problems.extend(_config_problems(node, spec, exc))

    if not triggers:
        problems.append("A flow needs exactly one trigger node; found none.")
    elif len(triggers) > 1:
        names = ", ".join(sorted(t.id for t in triggers))
        problems.append(f"A flow needs exactly one trigger node; found {len(triggers)}: {names}.")

    # --- edges -------------------------------------------------------------
    for edge in graph.edges:
        if edge.source not in id_set:
            problems.append(f"Edge from unknown node {edge.source!r}.")
        if edge.target not in id_set:
            problems.append(f"Edge to unknown node {edge.target!r}.")
        if edge.source == edge.target:
            problems.append(
                f"{_who(graph.node(edge.source), edge.source)} cannot connect to itself."
            )

    for trigger in triggers:
        if graph.incoming(trigger.id):
            problems.append(f"{_who(trigger)} is the trigger; nothing can connect into it.")

    # An edge leaving a port the node does not have would draw fine and never
    # fire: the engine only follows ports the node reports as fired.
    for edge in graph.edges:
        spec = known_types.get(graph.node(edge.source).type) if graph.node(edge.source) else None
        if spec is None:
            continue
        port = edge.source_handle or "out"
        ports = tuple(getattr(spec, "ports", ()) or ("out",))
        if port not in ports:
            problems.append(
                f"{_who(graph.node(edge.source))} has no output port {port!r}; "
                f"it has {', '.join(ports)}."
            )

    # A handover edge means "another agent takes the conversation", so the thing
    # on the other end has to be an agent. Wired to a render or a post it would
    # look connected on the canvas and simply never fire, because the agent is
    # offered a transfer tool per *agent* downstream and would find none.
    by_id = {node.id: node for node in graph.nodes}
    for edge in graph.edges:
        if edge.source_handle != HANDOVER_PORT:
            continue
        target = by_id.get(edge.target)
        if target is not None and target.type != AGENT_TYPE:
            problems.append(
                f"The handover from {_who(by_id.get(edge.source), edge.source)} goes to "
                f"{_who(target)}, which is a "
                f"{target.type}. Handover passes a conversation to another agent, so it "
                "can only connect to an AI Agent."
            )

    if problems:
        raise GraphError(problems)

    # --- shape -------------------------------------------------------------
    # Cycles are checked before reachability: an unreachable-looking node is
    # usually a symptom of a cycle, and reporting both is confusing.
    if cycle := find_cycle(graph):
        names = [_who(graph.node(node_id), node_id) for node_id in cycle]
        raise GraphError([f"The flow contains a loop: {' → '.join(names)}."])

    # One input, one connection. The engine can merge several upstreams into a
    # dict, but a node fed by two things is a node whose input nobody can
    # predict from the canvas, and the editor refuses the second connection as
    # it is dragged. This is the same rule at save time, for graphs that did
    # not come through the editor. Checked after cycles: a loop back into a
    # chain always gives some node two inputs, and the loop is the real news.
    for node in graph.nodes:
        sources = [edge.source for edge in graph.incoming(node.id)]
        if len(sources) > 1:
            problems.append(
                f"{_who(node)} has {len(sources)} inputs "
                f"({', '.join(sorted(_who(graph.node(s), s) for s in sources))}). "
                "A node takes its input from one connection."
            )

    trigger_id = triggers[0].id
    reachable = reachable_from(graph, trigger_id)
    orphans = sorted(id_set - reachable)
    if orphans:
        problems.append(
            "These nodes are not connected to the trigger and would never run: "
            + ", ".join(_who(graph.node(o), o) for o in orphans)
            + "."
        )

    if problems:
        raise GraphError(problems)


def find_cycle(graph: Graph) -> list[str] | None:
    """Return one cycle as a readable path, or None.

    Kahn's algorithm would say *that* a cycle exists; the author needs to know
    *where*, so this walks it and returns the actual loop.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source].append(edge.target)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n.id: WHITE for n in graph.nodes}
    stack: list[str] = []

    def walk(node_id: str) -> list[str] | None:
        colour[node_id] = GREY
        stack.append(node_id)
        for nxt in adjacency.get(node_id, []):
            if colour.get(nxt) == GREY:
                return stack[stack.index(nxt) :] + [nxt]
            if colour.get(nxt) == WHITE:
                if found := walk(nxt):
                    return found
        stack.pop()
        colour[node_id] = BLACK
        return None

    for node in graph.nodes:
        if colour[node.id] == WHITE:
            if found := walk(node.id):
                return found
    return None


def reachable_from(graph: Graph, start: str) -> set[str]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source].append(edge.target)

    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def topological_order(graph: Graph) -> list[str]:
    """Execution order. Assumes `validate_graph` already ruled out cycles."""
    indegree: dict[str, int] = {n.id: 0 for n in graph.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source].append(edge.target)
        indegree[edge.target] += 1

    # Sorted so the order is deterministic across runs: two runs of the same
    # graph should produce logs that can be diffed against each other.
    ready = deque(sorted(n for n, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        current = ready.popleft()
        order.append(current)
        for nxt in sorted(adjacency.get(current, [])):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    return order
