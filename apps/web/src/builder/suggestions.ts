/**
 * What `{{ … }}` can refer to, computed for one selected node.
 *
 * The templating engine resolves data paths against four roots — `input`,
 * `nodes`, `vars`, `trigger` — and the editor knows enough of the graph to
 * offer real completions rather than making the author memorise them:
 *
 * - which nodes are *upstream* of the selected one (only those have output by
 *   the time it runs — suggesting a downstream node's output would be
 *   suggesting a value that cannot exist yet);
 * - each upstream node's declared `output_paths`, shipped by the node registry
 *   itself so the suggestions and the executor can never disagree about what
 *   an agent or webhook actually emits;
 * - variable names, read straight out of upstream Set nodes' assignments —
 *   the author already typed them once, so the editor should not ask twice.
 */

import type { Edge } from "@xyflow/react";

import type { FlowNode } from "./graph";
import type { NodeSpec } from "./specs";

export interface Suggestion {
  /** What gets inserted between the braces. */
  token: string;
  /** Where it comes from, shown dimmed beside the token. */
  hint: string;
}

/** Ancestors of `nodeId`, nearest first — parents, then grandparents, … */
function upstreamOf(nodeId: string, edges: Edge[]): string[] {
  const parents = new Map<string, string[]>();
  for (const edge of edges) {
    const list = parents.get(edge.target) ?? [];
    list.push(edge.source);
    parents.set(edge.target, list);
  }

  const seen = new Set<string>();
  const ordered: string[] = [];
  let frontier = parents.get(nodeId) ?? [];
  while (frontier.length > 0) {
    const next: string[] = [];
    for (const id of frontier) {
      if (seen.has(id)) continue;
      seen.add(id);
      ordered.push(id);
      next.push(...(parents.get(id) ?? []));
    }
    frontier = next;
  }
  return ordered;
}

export function buildSuggestions(
  selectedId: string,
  nodes: FlowNode[],
  edges: Edge[],
  specs: Map<string, NodeSpec>,
): Suggestion[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const ancestors = upstreamOf(selectedId, edges);
  const suggestions: Suggestion[] = [];

  // The immediate parent's output is `input` — the reference people reach for
  // first, so it leads the list.
  const [parent] = ancestors;
  if (parent) {
    const parentNode = byId.get(parent);
    const label = parentNode?.data.label ?? parent;
    suggestions.push({ token: "input", hint: `output of ${label}` });
    for (const path of specs.get(parentNode?.data.nodeType ?? "")?.output_paths ?? []) {
      suggestions.push({ token: `input.${path}`, hint: label });
    }
  }

  for (const id of ancestors) {
    const node = byId.get(id);
    if (!node) continue;
    suggestions.push({ token: `nodes.${id}.output`, hint: node.data.label });
    for (const path of specs.get(node.data.nodeType)?.output_paths ?? []) {
      suggestions.push({ token: `nodes.${id}.output.${path}`, hint: node.data.label });
    }

    // Set-node assignments become `vars.<name>` at run time; the names are
    // sitting right there in its config.
    if (node.data.nodeType === "data.set") {
      const assignments = node.data.config.assignments;
      if (Array.isArray(assignments)) {
        for (const assignment of assignments) {
          const name = (assignment as { name?: unknown })?.name;
          if (typeof name === "string" && name.trim()) {
            suggestions.push({ token: `vars.${name.trim()}`, hint: node.data.label });
          }
        }
      }
    }
  }

  suggestions.push({ token: "trigger.payload", hint: "what started the run" });
  suggestions.push({ token: "run.id", hint: "this run's id" });
  return suggestions;
}
