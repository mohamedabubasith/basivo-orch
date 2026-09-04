/**
 * The node palette, fetched from the engine rather than duplicated here.
 *
 * `GET /api/v1/nodes` is generated from the same `_NODES` tuple the executor
 * dispatches on, so a node cannot appear in this UI that the engine would
 * reject, and one cannot be added to the engine and go missing from the UI.
 * Fetched once per session — the registry only changes when the API is
 * redeployed, at which point the page is reloaded anyway.
 */

import { api } from "../lib/api";

export interface NodeSpec {
  type: string;
  label: string;
  description: string;
  tier: number;
  category: string;
  is_trigger: boolean;
  /** The guide behind the palette's info button: when to use it, what it needs, a typical chain. */
  when: string;
  needs: string[];
  example: string;
  ports: string[];
  output_paths: string[];
  config_schema: Record<string, unknown>;
}

let cached: Promise<NodeSpec[]> | null = null;

export function loadSpecs(): Promise<NodeSpec[]> {
  cached ??= api.get<NodeSpec[]>("/api/v1/nodes");
  return cached;
}

/** Defaults from the schema, so a freshly dropped node is as valid as it can be. */
export function initialConfig(spec: NodeSpec): Record<string, unknown> {
  const properties = (spec.config_schema.properties ?? {}) as Record<
    string,
    { default?: unknown }
  >;
  const config: Record<string, unknown> = {};
  for (const [key, schema] of Object.entries(properties)) {
    if (schema.default !== undefined && schema.default !== null)
      config[key] = schema.default;
  }
  return config;
}

/** Palette grouping. Triggers first — a flow cannot run without exactly one. */
export function groupSpecs(
  specs: NodeSpec[],
): { heading: string; specs: NodeSpec[] }[] {
  const triggers = specs.filter((spec) => spec.is_trigger);
  const rest = specs.filter((spec) => !spec.is_trigger);
  const byCategory = new Map<string, NodeSpec[]>();
  for (const spec of rest) {
    const heading = title(spec.category);
    const list = byCategory.get(heading) ?? [];
    list.push(spec);
    byCategory.set(heading, list);
  }
  return [
    ...(triggers.length ? [{ heading: "Triggers", specs: triggers }] : []),
    ...[...byCategory.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([heading, list]) => ({ heading, specs: list })),
  ];
}

/** Palette headings. The engine's category names are for grouping, not reading. */
const HEADINGS: Record<string, string> = {
  utility: "Logic & Data",
  data: "Logic & Data",
  ai: "AI",
  design: "Images, Video & Voice",
  devops: "Code & Git",
  social: "Messaging",
};

function title(value: string): string {
  return HEADINGS[value] ?? value.charAt(0).toUpperCase() + value.slice(1);
}
