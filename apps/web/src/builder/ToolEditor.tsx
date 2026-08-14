/**
 * The Agent node's tool editor — structured, not a JSON textarea.
 *
 * A tool is the author's own contract: its name, what it does, what arguments
 * the model may pass, and where the call goes. Asking someone to hand-write
 * that as raw JSON in a 4-row textarea meant the most personal part of the
 * agent was also the easiest to get syntactically wrong, with nothing but a
 * parse error to say so. This editor keeps the same stored shape (the node
 * config is unchanged — see `ToolDefinition` in `agent.py`) and swaps only how
 * it is authored: one card per tool, plain fields for the parts that are
 * names and URLs, and a small parameter table that *generates* the JSON
 * Schema, since "the model may pass a city string" should not require knowing
 * what JSON Schema is.
 */

import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import { cx } from "../lib/cx";

/** Mirrors `ToolDefinition` in `basivo_orch/flows/nodes/agent.py`. */
export interface ToolValue {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  kind: "http" | "constant";
  url: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  headers: Record<string, string>;
  body: unknown;
  timeout_seconds: number;
  value: unknown;
}

interface Parameter {
  name: string;
  type: "string" | "number" | "integer" | "boolean";
  description: string;
  required: boolean;
}

const SMALL_INPUT =
  "w-full rounded-md border border-ink-700 bg-ink-950/60 px-2 py-1.5 text-xs text-ink-100 outline-none focus:border-brand-400";

/** `input_schema` -> rows. The schema is the storage format; rows are the UI. */
function schemaToParameters(schema: Record<string, unknown> | undefined): Parameter[] {
  const properties = (schema?.properties ?? {}) as Record<
    string,
    { type?: string; description?: string }
  >;
  const required = new Set((schema?.required as string[] | undefined) ?? []);
  return Object.entries(properties).map(([name, prop]) => ({
    name,
    type: (["string", "number", "integer", "boolean"].includes(prop.type ?? "")
      ? prop.type
      : "string") as Parameter["type"],
    description: prop.description ?? "",
    required: required.has(name),
  }));
}

function parametersToSchema(parameters: Parameter[]): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  for (const parameter of parameters) {
    if (!parameter.name.trim()) continue;
    properties[parameter.name.trim()] = {
      type: parameter.type,
      ...(parameter.description.trim() ? { description: parameter.description.trim() } : {}),
    };
  }
  const required = parameters
    .filter((parameter) => parameter.required && parameter.name.trim())
    .map((parameter) => parameter.name.trim());
  return {
    type: "object",
    properties,
    ...(required.length ? { required } : {}),
  };
}

function emptyTool(existing: ToolValue[]): ToolValue {
  let name = "my_tool";
  let n = 1;
  while (existing.some((tool) => tool.name === name)) name = `my_tool_${++n}`;
  return {
    name,
    description: "",
    input_schema: { type: "object", properties: {} },
    kind: "http",
    url: "",
    method: "POST",
    headers: {},
    body: null,
    timeout_seconds: 30,
    value: null,
  };
}

export function ToolEditor({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (tools: ToolValue[]) => void;
}) {
  const tools: ToolValue[] = Array.isArray(value) ? (value as ToolValue[]) : [];
  const [open, setOpen] = useState<number | null>(null);

  function update(index: number, patch: Partial<ToolValue>) {
    onChange(tools.map((tool, i) => (i === index ? { ...tool, ...patch } : tool)));
  }

  function remove(index: number) {
    onChange(tools.filter((_, i) => i !== index));
    setOpen(null);
  }

  return (
    <div className="space-y-2">
      <AnimatePresence initial={false}>
        {tools.map((tool, index) => (
          <motion.div
            key={index}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="relative overflow-hidden rounded-lg border border-ink-700/70 bg-ink-950/40"
          >
            <button
              type="button"
              onClick={() => setOpen(open === index ? null : index)}
              className="flex w-full items-center gap-2 py-2 pr-16 pl-3 text-left"
            >
              <span
                className="h-1.5 w-1.5 flex-none rounded-full"
                style={{
                  backgroundColor:
                    tool.kind === "constant" ? "var(--status-warn)" : "var(--series)",
                }}
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-200">
                {tool.name || "unnamed"}
              </span>
              <span className="flex-none text-[0.62rem] text-ink-500 uppercase">
                {tool.kind === "constant" ? "stub" : tool.method}
              </span>
              <svg
                viewBox="0 0 24 24"
                className={cx(
                  "h-3.5 w-3.5 flex-none text-ink-500 transition-transform",
                  open === index && "rotate-180",
                )}
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M6 9l6 6 6-6"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
            </button>

            {/* On the header row, not buried inside the expanded card —
                deleting a tool must not require opening it first. A sibling of
                the header button (absolutely positioned over it) rather than a
                child, because buttons cannot nest. */}
            <button
              type="button"
              aria-label={`Remove tool ${tool.name}`}
              title="Remove this tool"
              onClick={() => remove(index)}
              className="absolute top-1.5 right-8 rounded-md p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-[var(--status-bad)]"
            >
              <TrashIcon />
            </button>

            {open === index && (
              <div className="space-y-3 border-t border-ink-700/60 p-3">
                <LabelledSmall label="Name" hint="How the model refers to it. Letters, digits, - and _.">
                  <input
                    value={tool.name}
                    onChange={(event) =>
                      update(index, { name: event.target.value.replace(/[^a-zA-Z0-9_-]/g, "_") })
                    }
                    className={cx(SMALL_INPUT, "font-mono")}
                  />
                </LabelledSmall>

                <LabelledSmall
                  label="Description"
                  hint="Tell the model when and why to call it — this is the whole basis of its decision."
                >
                  <textarea
                    value={tool.description}
                    rows={2}
                    onChange={(event) => update(index, { description: event.target.value })}
                    className={cx(SMALL_INPUT, "resize-y")}
                  />
                </LabelledSmall>

                <ParameterTable
                  schema={tool.input_schema}
                  onChange={(schema) => update(index, { input_schema: schema })}
                />

                <LabelledSmall label="Kind">
                  <select
                    value={tool.kind}
                    onChange={(event) =>
                      update(index, { kind: event.target.value as ToolValue["kind"] })
                    }
                    className={SMALL_INPUT}
                  >
                    <option value="http">HTTP call</option>
                    <option value="constant">Constant value (stub for testing)</option>
                  </select>
                </LabelledSmall>

                {tool.kind === "http" ? (
                  <>
                    <div className="flex gap-2">
                      <select
                        value={tool.method}
                        onChange={(event) =>
                          update(index, { method: event.target.value as ToolValue["method"] })
                        }
                        className={cx(SMALL_INPUT, "w-24 flex-none")}
                      >
                        {["GET", "POST", "PUT", "PATCH", "DELETE"].map((method) => (
                          <option key={method} value={method}>
                            {method}
                          </option>
                        ))}
                      </select>
                      <input
                        value={tool.url}
                        placeholder="https://api.example.com/x?q={{ tool.city }}"
                        onChange={(event) => update(index, { url: event.target.value })}
                        className={cx(SMALL_INPUT, "font-mono")}
                      />
                    </div>
                    <p className="text-[0.64rem] leading-relaxed text-ink-500">
                      The model&rsquo;s arguments are available as{" "}
                      <code className="text-ink-400">{"{{ tool.<name> }}"}</code> in the URL, and
                      are sent as the JSON body on non-GET calls.
                    </p>
                  </>
                ) : (
                  <LabelledSmall
                    label="Returned value"
                    hint="Returned verbatim on every call — useful for wiring a flow before the real endpoint exists."
                  >
                    <input
                      value={typeof tool.value === "string" ? tool.value : JSON.stringify(tool.value ?? "")}
                      onChange={(event) => update(index, { value: event.target.value })}
                      className={cx(SMALL_INPUT, "font-mono")}
                    />
                  </LabelledSmall>
                )}

              </div>
            )}
          </motion.div>
        ))}
      </AnimatePresence>

      <button
        type="button"
        onClick={() => {
          onChange([...tools, emptyTool(tools)]);
          setOpen(tools.length);
        }}
        className="w-full rounded-lg border border-dashed border-ink-600 px-3 py-2 text-xs text-ink-400 transition-colors hover:border-brand-400 hover:text-ink-200"
      >
        + Add a tool
      </button>
    </div>
  );
}

function ParameterTable({
  schema,
  onChange,
}: {
  schema: Record<string, unknown> | undefined;
  onChange: (schema: Record<string, unknown>) => void;
}) {
  // Draft rows live in state; the schema receives only completed ones.
  //
  // The first version derived rows from the schema on every render — a clean
  // single-source-of-truth idea with a fatal loop: `parametersToSchema` drops
  // rows with no name (an empty property key is not valid schema), so the
  // blank row "+ Add parameter" created was erased by its own commit before
  // it could be typed into. Adding a parameter was literally impossible.
  // State holds what the person is typing; the schema holds what is finished.
  const [parameters, setParameters] = useState<Parameter[]>(() => schemaToParameters(schema));

  function commit(next: Parameter[]) {
    setParameters(next);
    onChange(parametersToSchema(next));
  }

  return (
    <div>
      <p className="mb-1 text-[0.68rem] font-medium text-ink-300">
        Parameters <span className="font-normal text-ink-500">— what the model may pass</span>
      </p>
      <div className="space-y-1.5">
        {parameters.map((parameter, index) => (
          <div key={index} className="flex items-center gap-1.5">
            <input
              value={parameter.name}
              placeholder="name"
              onChange={(event) =>
                commit(
                  parameters.map((p, i) =>
                    i === index
                      ? { ...p, name: event.target.value.replace(/[^a-zA-Z0-9_]/g, "_") }
                      : p,
                  ),
                )
              }
              className={cx(SMALL_INPUT, "w-24 flex-none font-mono")}
            />
            <select
              value={parameter.type}
              onChange={(event) =>
                commit(
                  parameters.map((p, i) =>
                    i === index ? { ...p, type: event.target.value as Parameter["type"] } : p,
                  ),
                )
              }
              className={cx(SMALL_INPUT, "w-20 flex-none")}
            >
              <option value="string">text</option>
              <option value="number">number</option>
              <option value="integer">integer</option>
              <option value="boolean">yes/no</option>
            </select>
            <input
              value={parameter.description}
              placeholder="what it means"
              onChange={(event) =>
                commit(
                  parameters.map((p, i) =>
                    i === index ? { ...p, description: event.target.value } : p,
                  ),
                )
              }
              className={SMALL_INPUT}
            />
            <label
              className="flex flex-none cursor-pointer items-center gap-1 text-[0.62rem] text-ink-400"
              title="Required"
            >
              <input
                type="checkbox"
                checked={parameter.required}
                onChange={(event) =>
                  commit(
                    parameters.map((p, i) =>
                      i === index ? { ...p, required: event.target.checked } : p,
                    ),
                  )
                }
              />
              req
            </label>
            <button
              type="button"
              aria-label="Remove parameter"
              title="Remove parameter"
              onClick={() => commit(parameters.filter((_, i) => i !== index))}
              className="flex-none rounded-md border border-ink-700 p-1.5 text-ink-500 transition-colors hover:border-[var(--status-bad)] hover:text-[var(--status-bad)]"
            >
              <TrashIcon />
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={() =>
          commit([...parameters, { name: "", type: "string", description: "", required: false }])
        }
        className="mt-1.5 text-[0.68rem] text-ink-400 underline decoration-dotted underline-offset-2 hover:text-ink-200"
      >
        + Add parameter
      </button>
    </div>
  );
}

function LabelledSmall({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-[0.68rem] font-medium text-ink-300">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[0.64rem] leading-relaxed text-ink-500">{hint}</p>}
    </div>
  );
}


function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" aria-hidden="true">
      <path
        d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M7 6.5l.8 12a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-12M10 10.5v6M14 10.5v6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
