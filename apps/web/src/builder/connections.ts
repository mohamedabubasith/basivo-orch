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
import type { NodeSpec } from "./specs";

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

  // Several connections into one node are fine: the two branches of an If /
  // Else, or the agents on a hand over, are alternatives, and the node after
  // them runs on whichever fired. The same output wired to the same node
  // twice is the only slip worth refusing.
  const duplicate = edges.find(
    (edge) =>
      edge.source === source &&
      edge.target === target &&
      (edge.sourceHandle ?? "out") === port,
  );
  if (duplicate) {
    return `${from.data.label} is already connected to ${to.data.label}.`;
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

/**
 * What is wrong with the graph right now, before anyone presses Validate.
 *
 * The server's `validate_graph` is the authority; this is the subset that can
 * be judged from the canvas alone and is cheap enough to run on every change:
 * a missing trigger, nodes the trigger cannot reach, and required fields left
 * empty. Each is pinned to its node, and the summary lines go in the banner.
 */
export function liveProblems(
  nodes: readonly FlowNode[],
  edges: readonly Edge[],
  specs: ReadonlyMap<string, NodeSpec>,
): { byNode: Map<string, string>; summary: string[] } {
  const byNode = new Map<string, string>();
  const summary: string[] = [];
  if (nodes.length === 0) return { byNode, summary };

  const trigger = nodes.find((node) => node.data.isTrigger);
  if (!trigger) {
    summary.push("Add a trigger. Nothing starts this flow yet.");
  }

  for (const node of nodes) {
    if (node.data.isTrigger || !trigger) continue;
    if (!reaches(edges, trigger.id, node.id)) {
      byNode.set(node.id, "Not connected to the trigger, so it would never run.");
      summary.push(`${node.data.label} is not connected to the trigger.`);
    }
  }

  for (const node of nodes) {
    const spec = specs.get(node.data.nodeType);
    const required = (spec?.config_schema.required as string[] | undefined) ?? [];
    const properties = (spec?.config_schema.properties ?? {}) as Record<
      string,
      { title?: string; pattern?: string; "x-pattern-hint"?: string }
    >;
    for (const key of required) {
      const value = node.data.config[key];
      const empty =
        value === undefined ||
        value === null ||
        (typeof value === "string" && value.trim() === "") ||
        (Array.isArray(value) && value.length === 0);
      if (!empty) continue;
      const title =
        properties[key]?.title ??
        key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " ");
      if (!byNode.has(node.id)) byNode.set(node.id, `${title} is required.`);
      summary.push(`${node.data.label}: ${title} is required.`);
    }
    // A filled field in the wrong shape, e.g. a repo without its owner.
    for (const [key, schema] of Object.entries(properties)) {
      const value = node.data.config[key];
      if (typeof value !== "string" || value === "" || !schema.pattern) continue;
      if (new RegExp(schema.pattern).test(value)) continue;
      const title = schema.title ?? key;
      const hint = schema["x-pattern-hint"] ?? "not in the expected form";
      if (!byNode.has(node.id)) byNode.set(node.id, `${title} should be ${hint}.`);
      summary.push(`${node.data.label}: ${title} should be ${hint}.`);
    }
  }
  return { byNode, summary };
}
