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
