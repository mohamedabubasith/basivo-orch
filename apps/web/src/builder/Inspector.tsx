/**
 * The node configuration panel, generated from the API's JSON Schema.
 *
 * The alternative was a hand-written form per node type, which fails in a
 * specific and expensive way: the engine gains a node, the palette shows it
 * because the palette is already generated, and the panel renders nothing to
 * configure it with. Reading `config_schema` means a node added to `_NODES` in
 * Python is fully editable here without a line of TypeScript.
 *
 * The trade is that the forms are plain. That is the right trade for six node
 * types that are still changing; a node whose editing experience genuinely
 * needs bespoke UI can be special-cased later, and the generic path stays as
 * the floor rather than the ceiling.
 */

import { useState } from "react";

import { cx } from "../lib/cx";
import { NodeIconChip } from "./nodeIcons";
import type { Suggestion } from "./suggestions";
import { TemplateInput } from "./TemplateInput";
import { CredentialPicker, ModelPicker } from "./pickers";
import { SubAgentEditor } from "./SubAgentEditor";
import { MODEL_PROVIDERS, VCS_PROVIDERS } from "./providers";
import { ToolEditor } from "./ToolEditor";
import type { NodeSpec } from "./specs";

interface SchemaField {
  key: string;
  title: string;
  description?: string;
  type: string;
  enum?: string[];
  required: boolean;
  itemDef?: JsonSchema;
  default?: unknown;
}

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  enum?: string[];
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  anyOf?: JsonSchema[];
  additionalProperties?: boolean | JsonSchema;
}

/**
 * Resolve `$ref`, and collapse the `anyOf: [T, null]` pydantic emits for
 * optionals.
 *
 * Pydantic puts `title` and `description` on the *outer* `X | None` wrapper,
 * not on its concrete branch — `{"anyOf": [{"type": "number"}, {"type":
 * "null"}], "title": "Temperature"}"`. Keeping only the concrete branch (as an
 * earlier version of this did) discards that wrapper and with it the title —
 * every optional field on every node rendered labelled by its raw snake_case
 * key instead of its name. The outer schema's `title`/`description` win when
 * present; the concrete branch's are the fallback for the plain, non-optional
 * case this function also handles.
 */
function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (schema.$ref) {
    const name = schema.$ref.replace("#/$defs/", "");
    return resolve(root.$defs?.[name] ?? {}, root);
  }
  if (schema.anyOf) {
    const concrete = schema.anyOf.find((option) => option.type !== "null");
    if (concrete) {
      const resolved = resolve(concrete, root);
      return {
        ...resolved,
        title: schema.title ?? resolved.title,
        description: schema.description ?? resolved.description,
        default: schema.default,
      };
    }
  }
  return schema;
}

function fields(root: JsonSchema): SchemaField[] {
  const required = new Set(root.required ?? []);
  return Object.entries(root.properties ?? {}).map(([key, raw]) => {
    const schema = resolve(raw, root);
    return {
      key,
      title: schema.title ?? key,
      description: schema.description,
      type: schema.type ?? "string",
      enum: schema.enum,
      required: required.has(key),
      itemDef: schema.items ? resolve(schema.items, root) : undefined,
      default: schema.default,
    };
  });
}

export function Inspector({
  spec,
  name,
  config,
  problem,
  orgId,
  flowId,
  publicBase,
  isPublished,
  nextRunAt,
  suggestions = [],
  onRename,
  onChange,
  onDelete,
  onClose,
}: {
  spec: NodeSpec;
  name: string;
  config: Record<string, unknown>;
  problem?: string;
  /** Only needed for the Agent node's credential picker. */
  orgId?: string | null;
  /** For nodes whose config is about being called from outside — the webhook
      trigger prints its own invocation URL rather than making the user hunt
      for it. */
  flowId?: string;
  publicBase?: string;
  isPublished?: boolean;
  /** When the scheduler will next fire this flow, if it is armed. */
  nextRunAt?: string | null;
  /** What {{ … }} can refer to from this node — see suggestions.ts. */
  suggestions?: Suggestion[];
  onRename: (name: string) => void;
  onChange: (config: Record<string, unknown>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const schema = spec.config_schema as JsonSchema;
  const list = fields(schema);
  const isAgent = spec.type === "agent.llm";
  // The autofix node embeds an LLM config under the same field names the
  // Agent uses, so the provider/model/credential pickers apply to both.
  const usesLlm = isAgent || spec.type === "git.autofix";
  const usesGit =
    spec.type === "git.ticket" || spec.type === "git.autofix" || spec.type === "git.comment";

  function set(key: string, value: unknown) {
    const next = { ...config };
    // Deleting rather than storing undefined: the API applies its own defaults,
    // and an explicit null would override one.
    if (value === undefined || value === "") delete next[key];
    else next[key] = value;
    onChange(next);
  }

  return (
    <aside className="flex h-full w-[356px] flex-none flex-col border-l border-ink-800/70 bg-ink-900/60">
      <div className="flex items-center gap-3 border-b border-ink-800/70 p-4">
        <NodeIconChip type={spec.type} size={8} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-ink-100">{spec.label}</p>
          <p className="truncate font-mono text-[0.68rem] text-ink-500">{spec.type}</p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close inspector"
          className="rounded-lg p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
            <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="1.8" />
          </svg>
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        <p className="text-xs leading-relaxed text-ink-500">{spec.description}</p>

        {problem && (
          <p
            className="rounded-lg border p-2.5 text-xs leading-relaxed"
            style={{ borderColor: "color-mix(in oklab, var(--status-bad) 45%, transparent)", color: "var(--status-bad)" }}
          >
            {problem}
          </p>
        )}

        {spec.type === "trigger.schedule" && (
          <div className="rounded-lg border border-ink-700/70 bg-ink-950/40 p-3">
            <p className="text-[0.68rem] font-medium text-ink-300">This schedule</p>
            {nextRunAt ? (
              <>
                <p className="mt-1.5 font-mono text-[0.72rem] text-ink-100">
                  Next run {new Date(nextRunAt).toLocaleString()}
                </p>
                <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
                  Armed. The run worker fires it — nothing needs to call this
                  flow. Cron is read in the timezone below, so 6am stays 6am
                  across daylight saving.
                </p>
              </>
            ) : (
              <p className="mt-1 text-[0.68rem] leading-relaxed text-ink-500">
                {isPublished
                  ? "Not armed yet — publish again after setting the schedule, and the next run time appears here."
                  : "A schedule only runs once the flow is published. Publish, and the next run time appears here."}
              </p>
            )}
          </div>
        )}

        {spec.type === "trigger.webhook" && (
          <div className="rounded-lg border border-ink-700/70 bg-ink-950/40 p-3">
            <p className="text-[0.68rem] font-medium text-ink-300">This webhook's URL</p>
            {isPublished ? (
              <code className="mt-1.5 block truncate rounded-md bg-ink-950/60 px-2 py-1.5 font-mono text-[0.68rem] text-ink-200">
                {publicBase}/hooks/{flowId}
              </code>
            ) : (
              <p className="mt-1 text-[0.68rem] leading-relaxed text-ink-500">
                Appears after you publish — an unpublished flow has no stable
                URL for callers to depend on.
              </p>
            )}
            <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
              No API key — paste it straight into GitHub or GitLab webhook
              settings. The secret below authenticates every delivery
              (GitHub's <code className="text-ink-400">X-Hub-Signature-256</code>,
              GitLab's <code className="text-ink-400">X-Gitlab-Token</code>, or a
              plain <code className="text-ink-400">X-Webhook-Secret</code>), so
              this URL only answers while{" "}
              <em className="not-italic text-ink-300">Require signature</em> is
              on. The delivery arrives as{" "}
              <code className="text-ink-400">{"{{ input.body }}"}</code>;
              API-key endpoints live under the Endpoints button above.
            </p>
          </div>
        )}

        <Labelled label="Name" hint="Shown on the canvas and in the run log.">
          <input
            value={name}
            onChange={(event) => onRename(event.target.value)}
            className="w-full rounded-lg border border-ink-700 bg-ink-950/60 px-2.5 py-2 text-sm text-ink-100 outline-none focus:border-brand-400"
          />
        </Labelled>

        {list.map((field) => (
          <Labelled
            key={field.key}
            label={field.title}
            required={field.required}
            hint={field.description}
          >
            {spec.type === "trigger.webhook" && field.key === "methods" ? (
              <MethodPicker
                value={Array.isArray(config.methods) ? (config.methods as string[]) : ["POST"]}
                onChange={(methods) => set("methods", methods)}
              />
            ) : isAgent && field.key === "provider" ? (
              <select
                value={String(config.provider ?? MODEL_PROVIDERS[0].value)}
                onChange={(event) => set("provider", event.target.value)}
                className={INPUT}
              >
                {MODEL_PROVIDERS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : usesLlm &&
              (field.key === "credential_id" || field.key === "vision_credential_id") ? (
              // Both LLM credentials get the picker. Without this the vision
              // one renders as a text box asking for a UUID, which is not a
              // thing any person can supply.
              <CredentialPicker
                orgId={orgId}
                provider={String(
                  (field.key === "vision_credential_id" ? config.vision_provider : null) ??
                    config.provider ??
                    MODEL_PROVIDERS[0].value,
                )}
                value={String(config[field.key] ?? "")}
                onChange={(v) => set(field.key, v)}
              />
            ) : spec.type === "code.python" && field.key === "code" ? (
              <CodeArea value={String(config.code ?? "")} onChange={(v) => set("code", v)} />
            ) : isAgent && field.key === "team_mode" ? (
              <div>
                <select
                  value={String(config.team_mode ?? "delegate")}
                  onChange={(event) => set("team_mode", event.target.value)}
                  className={INPUT}
                >
                  <option value="delegate">Delegate — it asks, then answers itself</option>
                  <option value="handover">Handover — it transfers, they answer you</option>
                </select>
                <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
                  {config.team_mode === "handover"
                    ? "Control moves. The agent it transfers to replies directly and can transfer on again — right for triage."
                    : "This agent stays in charge: it asks, gets an answer back, and writes the reply itself — right for combining several answers."}
                </p>
              </div>
            ) : isAgent && field.key === "sub_agents" ? (
              <SubAgentEditor
                value={config.sub_agents}
                onChange={(agents) => set("sub_agents", agents)}
                orgId={orgId}
                parentProvider={String(config.provider ?? MODEL_PROVIDERS[0].value)}
                parentCredentialId={String(config.credential_id ?? "")}
              />
            ) : isAgent && field.key === "tools" ? (
              <ToolEditor value={config.tools} onChange={(tools) => set("tools", tools)} suggestions={suggestions} />
            ) : usesLlm && field.key === "vision_provider" ? (
              <select
                value={String(config.vision_provider ?? "")}
                onChange={(event) => set("vision_provider", event.target.value)}
                className={INPUT}
              >
                <option value="">Same as the repair model</option>
                {MODEL_PROVIDERS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : usesGit && field.key === "git_provider" ? (
              <select
                value={String(config.git_provider ?? "github")}
                onChange={(event) => set("git_provider", event.target.value)}
                className={INPUT}
              >
                {VCS_PROVIDERS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : usesGit && field.key === "git_credential_id" ? (
              <CredentialPicker
                orgId={orgId}
                provider={String(config.git_provider ?? "github")}
                value={String(config.git_credential_id ?? "")}
                onChange={(v) => set("git_credential_id", v)}
              />
            ) : (spec.type === "git.autofix" &&
                (field.key === "problem" || field.key === "instructions")) ||
              (spec.type === "git.comment" && field.key === "body") ? (
              <TemplateInput
                multiline
                rows={field.key === "problem" ? 4 : 3}
                value={String(config[field.key] ?? "")}
                onChange={(v) => set(field.key, v)}
                suggestions={suggestions}
                placeholder={field.key === "problem" ? "{{ input.error }}" : ""}
              />
            ) : isAgent && (field.key === "prompt" || field.key === "system") ? (
              <TemplateInput
                multiline
                rows={field.key === "prompt" ? 5 : 3}
                value={String(config[field.key] ?? "")}
                onChange={(v) => set(field.key, v)}
                suggestions={suggestions}
                placeholder={field.key === "prompt" ? "{{ input.text }}" : "You are…"}
              />
            ) : usesLlm && (field.key === "model" || field.key === "vision_model") ? (
              <ModelPicker
                orgId={orgId}
                credentialId={String(
                  (field.key === "vision_model" ? config.vision_credential_id : null) ??
                    config.credential_id ??
                    "",
                )}
                value={String(config[field.key] ?? "")}
                onChange={(v) => set(field.key, v)}
              />
            ) : (
              <FieldInput
                field={field}
                value={config[field.key]}
                onChange={(v) => set(field.key, v)}
                suggestions={suggestions}
              />
            )}
          </Labelled>
        ))}
      </div>

      <div className="border-t border-ink-800/70 p-4">
        <button
          onClick={onDelete}
          className="w-full rounded-lg border border-ink-700 px-3 py-2 text-sm transition-colors hover:border-[var(--status-bad)]"
          style={{ color: "var(--status-bad)" }}
        >
          Delete node
        </button>
      </div>
    </aside>
  );
}

function Labelled({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-[0.7rem] font-medium tracking-wide text-ink-300">
        {label}
        {required && <span style={{ color: "var(--status-warn)" }}> *</span>}
      </label>
      {children}
      {hint && <p className="mt-1 text-[0.68rem] leading-relaxed text-ink-500">{hint}</p>}
    </div>
  );
}

const INPUT =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-2.5 py-2 text-sm text-ink-100 outline-none focus:border-brand-400";

/**
 * A concrete example rather than an empty `[]`. The Agent node's `tools`
 * field is an array of objects with its own nested schema — the generic
 * form here does not build a sub-form per tool, so what stands in for one is
 * a worked example a person can copy and edit, showing every field a tool
 * can carry: an HTTP call, and the constant-value stub used to fake one.
 */
const TOOLS_EXAMPLE = JSON.stringify(
  [
    {
      name: "get_weather",
      description: "Look up the current weather for a city.",
      input_schema: {
        type: "object",
        properties: { city: { type: "string" } },
        required: ["city"],
      },
      kind: "http",
      url: "https://api.example.com/weather?city={{ tool.city }}",
      method: "GET",
    },
    {
      name: "stub_example",
      description: "Always returns the same value — useful while testing a flow.",
      input_schema: { type: "object", properties: {} },
      kind: "constant",
      value: "ok",
    },
  ],
  null,
  1,
);

function FieldInput({
  field,
  value,
  onChange,
  suggestions = [],
}: {
  field: SchemaField;
  value: unknown;
  onChange: (value: unknown) => void;
  suggestions?: Suggestion[];
}) {
  if (field.enum) {
    return (
      <select
        value={String(value ?? field.default ?? field.enum[0])}
        onChange={(event) => onChange(event.target.value)}
        className={INPUT}
      >
        {field.enum.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "boolean") {
    const checked = Boolean(value ?? field.default);
    return (
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          onClick={() => onChange(!checked)}
          className={cx(
            "relative h-6 w-11 flex-none rounded-full border transition-colors",
            // ink-600 + an ink-100 knob, not ink-700 + white: in light mode
            // ink-700 is #dfe3ed and a white knob on it is invisible — the
            // switch read as an empty pill with no state at all.
            checked ? "border-brand-500 bg-brand-500" : "border-ink-500 bg-ink-600",
          )}
        >
          <span
            className={cx(
              "absolute top-[3px] h-4 w-4 rounded-full transition-transform",
              checked ? "translate-x-6 bg-white" : "translate-x-1 bg-ink-100",
            )}
          />
        </button>
        {/* The state in a word, so the toggle is never a colour-only guess. */}
        <span className={cx("text-xs", checked ? "text-ink-200" : "text-ink-500")}>
          {checked ? "On" : "Off"}
        </span>
      </div>
    );
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <input
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={field.default === undefined || field.default === null ? "" : String(field.default)}
        onChange={(event) =>
          onChange(event.target.value === "" ? undefined : Number(event.target.value))
        }
        className={INPUT}
      />
    );
  }

  // Objects and arrays are edited as JSON. A bespoke row editor per shape is
  // the eventual answer; until then this is at least honest about what it is,
  // and it refuses to hand back invalid JSON rather than saving a broken config.
  if (field.type === "object" || field.type === "array") {
    return (
      <JsonInput
        value={value}
        placeholder={field.key === "tools" ? TOOLS_EXAMPLE : field.type === "array" ? "[]" : "{}"}
        onChange={onChange}
      />
    );
  }

  return (
    <TemplateInput
      value={value === undefined || value === null ? "" : String(value)}
      placeholder={
        field.default === undefined || field.default === null ? "" : String(field.default)
      }
      onChange={(v) => onChange(v)}
      suggestions={suggestions}
      mono
    />
  );
}

function JsonInput({
  value,
  placeholder,
  onChange,
}: {
  value: unknown;
  placeholder: string;
  onChange: (value: unknown) => void;
}) {
  const [text, setText] = useState(() =>
    value === undefined || value === null ? "" : JSON.stringify(value, null, 2),
  );
  const [invalid, setInvalid] = useState(false);

  return (
    <>
      <textarea
        value={text}
        rows={4}
        spellCheck={false}
        placeholder={placeholder}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          if (next.trim() === "") {
            setInvalid(false);
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(next));
            setInvalid(false);
          } catch {
            // Held back deliberately: committing unparseable text would either
            // save a broken config or silently discard what was typed.
            setInvalid(true);
          }
        }}
        className={cx(
          INPUT,
          "resize-y font-mono text-[0.75rem]",
          invalid && "border-[var(--status-bad)] focus:border-[var(--status-bad)]",
        )}
      />
      {invalid && (
        <p className="mt-1 text-[0.68rem]" style={{ color: "var(--status-bad)" }}>
          Not valid JSON — the last valid value is still saved.
        </p>
      )}
    </>
  );
}

function CodeArea({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <textarea
      value={value}
      rows={16}
      spellCheck={false}
      autoCorrect="off"
      autoCapitalize="off"
      placeholder={'def main(data):\n    # data has: input, nodes, vars, trigger\n    return {"ok": True}'}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        event.preventDefault();
        const target = event.currentTarget;
        const { selectionStart, selectionEnd } = target;
        const next = value.slice(0, selectionStart) + "    " + value.slice(selectionEnd);
        onChange(next);
        // The value update is async through React; restore the caret after it.
        requestAnimationFrame(() => {
          target.selectionStart = target.selectionEnd = selectionStart + 4;
        });
      }}
      className={cx(
        INPUT,
        "resize-y font-mono text-[0.78rem] leading-relaxed whitespace-pre",
      )}
    />
  );
}


/**
 * HTTP methods as checkboxes. The generic form rendered this list-of-enums as
 * a JSON textarea — asking someone to type ["POST","PUT"] by hand, quotes and
 * all, to answer a five-option multiple choice.
 */
function MethodPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (methods: string[]) => void;
}) {
  const ALL = ["GET", "POST", "PUT", "PATCH", "DELETE"];
  return (
    <div className="flex flex-wrap gap-1.5">
      {ALL.map((method) => {
        const active = value.includes(method);
        return (
          <button
            key={method}
            type="button"
            aria-pressed={active}
            onClick={() => {
              const next = active ? value.filter((m) => m !== method) : [...value, method];
              // Zero methods is a webhook nothing can call; refuse the last
              // uncheck rather than saving a config that can never fire.
              if (next.length > 0) onChange(next);
            }}
            className={cx(
              "rounded-lg border px-2.5 py-1.5 font-mono text-[0.7rem] transition-colors",
              active
                ? "border-brand-400 bg-brand-500/15 text-ink-100"
                : "border-ink-700 text-ink-400 hover:border-ink-500",
            )}
          >
            {method}
          </button>
        );
      })}
    </div>
  );
}
