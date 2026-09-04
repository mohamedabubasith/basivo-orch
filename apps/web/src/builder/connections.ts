/**
 * What the canvas refuses to wire, and why.
 *
 * The engine's `validate_graph` (graph.py) rejects the same things at save
 * time. Checking here as the user drags means the handle simply will not take
 * the connection, and the banner says why, instead of a finished-looking graph
 * failing when they press Validate. Keep the two lists in step: every rule
 * below has a twin on the server, and the server's wins.
 */

import type { Edge } from "@xyflow/react";

import type { FlowNode } from "./graph";

export const HANDOVER_PORT = "handover";
export const AGENT_TYPE = "agent.llm";

/** Null when the connection is allowed; otherwise one sentence for the user. */
export function connectionProblem(
  // Both a `Connection` mid-drag and an `Edge` fit this shape.
  connection: {
    source: string | null;
    target: string | null;
    sourceHandle?: string | null;
  },
  nodes: readonly FlowNode[],
  edges: readonly Edge[],
): string | null {
  const { source, target } = connection;
  if (!source || !target) return "Drop the connection on a node's input.";
  if (source === target) return "A node cannot connect to itself.";

  const from = nodes.find((node) => node.id === source);
  const to = nodes.find((node) => node.id === target);
  if (!from || !to) return "That node no longer exists.";

  if (to.data.isTrigger) {
    return "A trigger starts the flow, so nothing can connect into it.";
  }

  const port = connection.sourceHandle ?? "out";
  const ports = from.data.ports.length > 0 ? from.data.ports : ["out"];
  if (!ports.includes(port)) {
    return `${from.data.label} has no "${port}" output.`;
  }

  // One input, one connection. Two things feeding a node would leave it
  // unclear which one it runs on, so the canvas refuses rather than merging.
  const taken = edges.find((edge) => edge.target === target);
  if (taken) {
    const other = nodes.find((node) => node.id === taken.source);
    return `${to.data.label} already takes its input from ${
      other?.data.label ?? taken.source
    }. Delete that connection first.`;
  }

  if (port === HANDOVER_PORT && to.data.nodeType !== AGENT_TYPE) {
    return `Handover passes the conversation to another agent, so it can only connect to an AI Agent, not ${to.data.label}.`;
  }

  if (reaches(edges, target, source)) {
    return `Connecting ${from.data.label} to ${to.data.label} would make a loop, and a flow has to end.`;
  }

  return null;
}

/** Whether `from` can reach `to` along existing edges. */
function reaches(edges: readonly Edge[], from: string, to: string): boolean {
  const seen = new Set<string>();
  const stack = [from];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current === to) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const edge of edges) {
      if (edge.source === current) stack.push(edge.target);
    }
  }
  return false;
}
