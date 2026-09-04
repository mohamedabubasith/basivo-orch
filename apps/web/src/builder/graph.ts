/**
 * The graph the API stores, and its translation to and from the canvas.
 *
 * These are two different shapes for the same thing and the translation has to
 * be lossless in both directions, because a round trip through the editor is
 * how every saved flow gets written. Anything this file drops — a config key it
 * does not know about, a port name it did not expect — is silently deleted from
 * the user's flow the next time they press save. So the conversion carries
 * whole objects rather than picking fields out of them.
 */

import type { Edge, Node } from "@xyflow/react";

/** A node exactly as `basivo_orch/flows/graph.py` defines it. */
export interface GraphNode {
  id: string;
  type: string;
  name: string | null;
  config: Record<string, unknown>;
  /** Canvas coordinates. The engine carries them through untouched. */
  position: { x?: number; y?: number };
}

export interface GraphEdge {
  source: string;
  target: string;
  /** Condition nodes emit "true"/"false"; everything else uses "out". */
  source_handle: string | null;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

/** What a canvas node carries. Kept flat so the inspector can edit it in place. */
export interface FlowNodeData extends Record<string, unknown> {
  label: string;
  nodeType: string;
  config: Record<string, unknown>;
  ports: string[];
  isTrigger: boolean;
  /** Set by validation; rendered on the node so errors have a location. */
  problem?: string;
  /** Set by a test run. */
  runStatus?: "running" | "succeeded" | "failed" | "skipped";
  runDetail?: string;
}

export type FlowNode = Node<FlowNodeData, "basivo">;

export function toCanvas(
  graph: Graph,
  specs: Map<string, { label: string; ports: string[]; is_trigger: boolean }>,
): { nodes: FlowNode[]; edges: Edge[] } {
  const nodes: FlowNode[] = graph.nodes.map((node, index) => {
    const spec = specs.get(node.type);
    return {
      id: node.id,
      type: "basivo",
      // A node saved without coordinates still has to land somewhere a person
      // can find it, rather than stacking every one of them at the origin.
      position: {
        x: node.position?.x ?? 80 + (index % 3) * 280,
        y: node.position?.y ?? 80 + Math.floor(index / 3) * 160,
      },
      data: {
        label: node.name ?? spec?.label ?? node.type,
        nodeType: node.type,
        config: node.config ?? {},
        ports: spec?.ports ?? ["out"],
        isTrigger: spec?.is_trigger ?? false,
      },
    };
  });

  const edges: Edge[] = graph.edges.map((edge) => ({
    id: edgeId(edge.source, edge.target, edge.source_handle),
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_handle ?? "out",
    type: "basivo",
    // A moving dash along the line, not just a static connector — it reads as
    // "data flows this way" the instant the canvas renders, before anything
    // has actually run. @xyflow/react ships the dash-offset keyframes for
    // `.animated` in its own stylesheet; this is the flag that turns it on.
    animated: true,
    // Set here as well as in defaultEdgeOptions, because a per-edge `style`
    // overrides the default entirely — raising the default alone changed
    // nothing for any edge that had actually been drawn.
    style: { stroke: "var(--series)", strokeWidth: 2.5, opacity: 0.9 },
  }));

  return { nodes, edges };
}

export function toGraph(nodes: FlowNode[], edges: Edge[]): Graph {
  return {
    nodes: nodes.map((node) => ({
      id: node.id,
      type: node.data.nodeType,
      name: node.data.label,
      config: node.data.config,
      // Rounded because the canvas produces sub-pixel values on every drag,
      // and a graph that differs only in the ninth decimal place would mark
      // the flow dirty forever.
      position: {
        x: Math.round(node.position.x),
        y: Math.round(node.position.y),
      },
    })),
    edges: edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      source_handle: edge.sourceHandle ?? "out",
    })),
  };
}

export function edgeId(
  source: string,
  target: string,
  handle?: string | null,
): string {
  return `${source}:${handle ?? "out"}->${target}`;
}

/**
 * A node id the engine will accept and a human can read in a template.
 *
 * Ids appear in expressions as `nodes.<id>.output`, so they are restricted to
 * letters, digits, `-` and `_` — and they must be unique, which is why the
 * existing set is passed in rather than trusting a counter.
 */
export function makeNodeId(nodeType: string, existing: Set<string>): string {
  const base =
    nodeType.replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_|_$/g, "") || "node";
  let candidate = base;
  let n = 1;
  while (existing.has(candidate)) candidate = `${base}_${++n}`;
  return candidate;
}

/**
 * Attach validation problems to the nodes they belong to.
 *
 * The API returns prose — `Node 'http_request_2' (http.request) is
 * misconfigured: url ...` — because it is also read by people using the API
 * directly. Pulling the quoted id out means the editor can underline the node
 * instead of showing a list the author has to match up by eye.
 */
export function attachProblems(
  nodes: FlowNode[],
  problems: string[],
): FlowNode[] {
  // The server names nodes the way the author did (the node's name, which
  // defaults to its label). A problem belongs to the node whose name it opens
  // with. Two nodes with the same name both get it, which is the right
  // answer to "which Render Poster" when nobody renamed either.
  const owned = (node: FlowNode) =>
    problems.find((problem) => {
      const name = node.data.label;
      return (
        problem.startsWith(name + ":") ||
        problem.startsWith(name + " ") ||
        problem.includes(" " + name + ",") ||
        problem.endsWith(" " + name + ".")
      );
    });
  return nodes.map((node) => {
    const problem = problems.length ? owned(node) : undefined;
    return node.data.problem === problem
      ? node
      : { ...node, data: { ...node.data, problem } };
  });
}
