"""The node registry.

One place that knows every node type. The palette the UI renders, the
validation the editor runs, and the dispatch the engine performs all read from
here, so a node cannot exist in one and be missing from another.

Adding a node is: write the class, add it to `_NODES`. Nothing else.
"""

from __future__ import annotations

from typing import Any

from basivo_orch.flows.nodes.agent import AgentNode
from basivo_orch.flows.nodes.base import (
    DEFAULT_PORT,
    Node,
    NodeContext,
    NodeError,
    NodeResult,
    summarise,
)
from basivo_orch.flows.nodes.code import CodeNode
from basivo_orch.flows.nodes.gitops import AutofixNode, CommentNode, TicketNode
from basivo_orch.flows.nodes.http import HttpRequestNode, assert_public_url
from basivo_orch.flows.nodes.logic import FALSE_PORT, TRUE_PORT, ConditionNode, SetVariablesNode
from basivo_orch.flows.nodes.triggers import (
    ManualTriggerNode,
    ScheduleTriggerNode,
    WebhookTriggerNode,
)

_NODES: tuple[type[Node], ...] = (
    ManualTriggerNode,
    WebhookTriggerNode,
    ScheduleTriggerNode,
    HttpRequestNode,
    ConditionNode,
    SetVariablesNode,
    AgentNode,
    CodeNode,
    TicketNode,
    AutofixNode,
    CommentNode,
)

REGISTRY: dict[str, type[Node]] = {node.type: node for node in _NODES}

# A duplicate type would mean one node silently shadowing another, and the
# graph that used the shadowed one would start doing something else entirely.
if len(REGISTRY) != len(_NODES):
    raise RuntimeError("Two node classes declare the same `type`.")

#: Instantiated once. Nodes hold no per-run state — everything they need
#: arrives in NodeContext — so sharing them across runs is safe and avoids
#: constructing six objects per execution.
INSTANCES: dict[str, Node] = {node_type: cls() for node_type, cls in REGISTRY.items()}


def get(node_type: str) -> Node:
    try:
        return INSTANCES[node_type]
    except KeyError:
        raise NodeError(f"Unknown node type {node_type!r}.") from None


def palette() -> list[dict[str, Any]]:
    """Every node type, for the editor's palette."""
    return [cls.describe() for cls in _NODES]


__all__ = [
    "DEFAULT_PORT",
    "FALSE_PORT",
    "INSTANCES",
    "REGISTRY",
    "TRUE_PORT",
    "Node",
    "NodeContext",
    "NodeError",
    "NodeResult",
    "assert_public_url",
    "get",
    "palette",
    "summarise",
]
