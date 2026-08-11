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

/** Resolve `$ref`, and collapse the `anyOf: [T, null]` pydantic emits for optionals. */
function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (schema.$ref) {
    const name = schema.$ref.replace("#/$defs/", "");
    return resolve(root.$defs?.[name] ?? {}, root);
  }
  if (schema.anyOf) {
    const concrete = schema.anyOf.find((option) => option.type !== "null");
    if (concrete) return { ...resolve(concrete, root), default: schema.default };
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
  onRename,
  onChange,
  onDelete,
  onClose,
}: {
  spec: NodeSpec;
  name: string;
  config: Record<string, unknown>;
  problem?: string;
  onRename: (name: string) => void;
  onChange: (config: Record<string, unknown>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const schema = spec.config_schema as JsonSchema;
  const list = fields(schema);

  function set(key: string, value: unknown) {
    const next = { ...config };
    // Deleting rather than storing undefined: the API applies its own defaults,
    // and an explicit null would override one.
    if (value === undefined || value === "") delete next[key];
    else next[key] = value;
    onChange(next);
  }

  return (
    <aside className="flex h-full w-[340px] flex-none flex-col border-l border-ink-800/70 bg-ink-900/60">
      <div className="flex items-start justify-between gap-3 border-b border-ink-800/70 p-4">
        <div className="min-w-0">
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
            <FieldInput field={field} value={config[field.key]} onChange={(v) => set(field.key, v)} />
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
      <label className="mb-1.5 block text-xs font-medium text-ink-300">
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

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: SchemaField;
  value: unknown;
  onChange: (value: unknown) => void;
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
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cx(
          "relative h-6 w-11 rounded-full transition-colors",
          checked ? "bg-brand-500" : "bg-ink-700",
        )}
      >
        <span
          className={cx(
            "absolute top-1 h-4 w-4 rounded-full bg-white transition-transform",
            checked ? "translate-x-6" : "translate-x-1",
          )}
        />
      </button>
    );
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <input
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={field.default === undefined ? "" : String(field.default)}
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
    return <JsonInput value={value} placeholder={field.type === "array" ? "[]" : "{}"} onChange={onChange} />;
  }

  return (
    <input
      value={value === undefined || value === null ? "" : String(value)}
      placeholder={field.default === undefined ? "" : String(field.default)}
      onChange={(event) => onChange(event.target.value)}
      className={cx(INPUT, "font-mono text-[0.8rem]")}
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
