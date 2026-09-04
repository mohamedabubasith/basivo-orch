// Runs with `npm test`: node's own runner with type stripping, no framework.
import { test } from "node:test";
import assert from "node:assert/strict";

import { connectionProblem } from "./connections.ts";
import type { FlowNode } from "./graph.ts";
import type { Edge } from "@xyflow/react";

const node = (
  id: string,
  extra: Partial<FlowNode["data"]> = {},
): FlowNode => ({
  id,
  type: "basivo",
  position: { x: 0, y: 0 },
  data: {
    label: id,
    nodeType: "data.set",
    config: {},
    ports: ["out"],
    isTrigger: false,
    ...extra,
  },
});
const edge = (source: string, target: string, sourceHandle = "out"): Edge => ({
  id: `${source}:${sourceHandle}->${target}`,
  source,
  target,
  sourceHandle,
});

const trigger = node("t", { isTrigger: true, nodeType: "trigger.manual" });
const condition = node("c", { nodeType: "logic.condition", ports: ["true", "false"] });
const agent = node("a", { nodeType: "agent.llm", ports: ["out", "handover"] });
const agent2 = node("a2", { nodeType: "agent.llm", ports: ["out", "handover"] });
const reply = node("r");
const other = node("o");

test("a plain first connection is allowed", () => {
  assert.equal(connectionProblem({ source: "t", target: "r", sourceHandle: null }, [trigger, reply], []), null);
});

test("a node cannot connect to itself", () => {
  assert.match(connectionProblem({ source: "r", target: "r", sourceHandle: null }, [reply], [])!, /itself/);
});

test("nothing connects into a trigger", () => {
  assert.match(connectionProblem({ source: "r", target: "t", sourceHandle: null }, [trigger, reply], [])!, /trigger/);
});

test("an input that is already connected refuses a second connection", () => {
  const problem = connectionProblem(
    { source: "o", target: "r", sourceHandle: null },
    [trigger, reply, other],
    [edge("t", "r")],
  );
  assert.match(problem!, /already takes its input from t/);
});

test("the same source cannot connect twice to the same target through another port", () => {
  const problem = connectionProblem(
    { source: "c", target: "r", sourceHandle: "false" },
    [condition, reply],
    [edge("c", "r", "true")],
  );
  assert.match(problem!, /already takes its input/);
});

test("a port the source does not have is refused", () => {
  assert.match(connectionProblem({ source: "r", target: "o", sourceHandle: "true" }, [reply, other], [])!, /no "true" output/);
});

test("handover only reaches an agent", () => {
  assert.match(connectionProblem({ source: "a", target: "r", sourceHandle: "handover" }, [agent, reply], [])!, /AI Agent/);
  assert.equal(connectionProblem({ source: "a", target: "a2", sourceHandle: "handover" }, [agent, agent2], []), null);
});

test("a connection that would close a loop is refused", () => {
  const problem = connectionProblem(
    { source: "o", target: "r", sourceHandle: null },
    [reply, other],
    [edge("r", "o")],
  );
  assert.match(problem!, /loop/);
});

test("a longer loop is caught too", () => {
  const a = node("x"), b = node("y"), c = node("z");
  const problem = connectionProblem(
    { source: "z", target: "x", sourceHandle: null },
    [a, b, c],
    [edge("x", "y"), edge("y", "z")],
  );
  assert.match(problem!, /loop/);
});

import { liveProblems } from "./connections.ts";
import type { NodeSpec } from "./specs.ts";

const spec = (type: string, required: string[] = []): NodeSpec => ({
  type, label: type, description: "", tier: 1, category: "x", is_trigger: type.startsWith("trigger."),
  hidden: false, when: "", needs: [], example: "", ports: ["out"], output_paths: [],
  config_schema: { required, properties: Object.fromEntries(required.map((k) => [k, { title: k === "html" ? "Html" : undefined }])) },
});
const specs = new Map<string, NodeSpec>([
  ["trigger.manual", spec("trigger.manual")],
  ["design.render", spec("design.render", ["html"])],
  ["data.set", spec("data.set")],
]);

test("no trigger is the first thing said", () => {
  const poster = node("p", { nodeType: "design.render", label: "Render Poster" });
  const { summary, byNode } = liveProblems([poster], [], specs);
  assert.equal(summary[0], "Add a trigger. Nothing starts this flow yet.");
  assert.match(byNode.get("p")!, /Html is required/);
});

test("a node the trigger cannot reach is flagged on the node and in the summary", () => {
  const t = node("t", { isTrigger: true, nodeType: "trigger.manual" });
  const a = node("a", { label: "Tidy" }), b = node("b", { label: "Stranded" });
  const { summary, byNode } = liveProblems([t, a, b], [edge("t", "a")], specs);
  assert.match(byNode.get("b")!, /Not connected/);
  assert.equal(byNode.has("a"), false);
  assert.deepEqual(summary, ["Stranded is not connected to the trigger."]);
});

test("a wired, filled-in flow has nothing to say", () => {
  const t = node("t", { isTrigger: true, nodeType: "trigger.manual" });
  const p = node("p", { nodeType: "design.render", label: "Render Poster", config: { html: "<div/>" } });
  const { summary, byNode } = liveProblems([t, p], [edge("t", "p")], specs);
  assert.equal(summary.length, 0);
  assert.equal(byNode.size, 0);
});

test("a repo typed without its owner is flagged with the expected form", () => {
  const withPattern = new Map(specs);
  withPattern.set("git.autofix", {
    ...spec("git.autofix", ["repo"]),
    config_schema: {
      required: ["repo"],
      properties: {
        repo: { title: "Repository", pattern: "^[^/\\s]+/[^\\s]+$", "x-pattern-hint": "owner/name, for example acme/website" },
      },
    },
  });
  const t = node("t", { isTrigger: true, nodeType: "trigger.manual" });
  const fix = node("f", { nodeType: "git.autofix", label: "Fix Code and Open PR", config: { repo: "basivo-autofix-demo" } });
  const { byNode } = liveProblems([t, fix], [edge("t", "f")], withPattern);
  assert.equal(byNode.get("f"), "Repository should be owner/name, for example acme/website.");
  fix.data.config.repo = "acme/website";
  assert.equal(liveProblems([t, fix], [edge("t", "f")], withPattern).byNode.size, 0);
});
