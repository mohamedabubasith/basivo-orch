/**
 * The panel behind "Test run".
 *
 * It used to be a textarea containing `{}`. That is technically everything the
 * API needs and practically useless: the shape a trigger expects is not
 * guessable, `{{ input.body.issue.number }}` in a node means nothing until
 * something like it exists in the payload, and a typo in raw JSON fails the run
 * rather than the form.
 *
 * So three things. Presets that match the trigger this flow actually has, a
 * Fields mode that builds the JSON from key and value rows, and a list of the
 * references this payload makes available — which is the bit that turns testing
 * into a conversation with the node editor rather than a guess.
 */

import { useMemo, useState } from "react";

import { Button } from "../components/ui";
import { cx } from "../lib/cx";

interface Preset {
  label: string;
  hint: string;
  payload: unknown;
}

/** Realistic starting points, by the trigger the flow is built on. */
function presetsFor(triggerType: string | undefined): Preset[] {
  if (triggerType === "trigger.webhook") {
    return [
      {
        label: "GitHub issue opened",
        hint: "What GitHub posts when someone files an issue",
        payload: {
          body: {
            action: "opened",
            issue: {
              number: 42,
              title: "Saving a flow loses the last edge",
              body: "Draw three nodes, connect them, reload. The third edge is gone.",
              user: { login: "ada" },
            },
            repository: { full_name: "your-org/your-repo" },
          },
        },
      },
      {
        label: "Plain webhook",
        hint: "A simple JSON body from your own service",
        payload: { body: { text: "Something happened worth acting on." } },
      },
    ];
  }
  if (triggerType === "trigger.schedule") {
    return [
      {
        label: "Scheduled fire",
        hint: "What the scheduler sends. Nothing is required",
        payload: {},
      },
    ];
  }
  return [
    {
      label: "A line of text",
      hint: "Read by the first node as {{ input.text }}",
      payload: { text: "Summarise the last release for our changelog." },
    },
    { label: "Empty", hint: "No input at all", payload: {} },
  ];
}

/** Rows the Fields mode edits. Dotted keys build nested objects. */
interface Row {
  key: string;
  value: string;
}

function rowsToPayload(rows: Row[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const { key, value } of rows) {
    const path = key.split(".").filter(Boolean);
    if (!path.length) continue;
    let cursor: Record<string, unknown> = out;
    path.slice(0, -1).forEach((segment) => {
      if (typeof cursor[segment] !== "object" || cursor[segment] === null) {
        cursor[segment] = {};
      }
      cursor = cursor[segment] as Record<string, unknown>;
    });
    // Numbers, booleans and JSON stay typed; everything else is a string. A
    // quoted "42" reaching a node as the number 42 would be its own surprise.
    let parsed: unknown = value;
    if (/^-?\d+(\.\d+)?$/.test(value.trim())) parsed = Number(value);
    else if (value.trim() === "true" || value.trim() === "false")
      parsed = value.trim() === "true";
    else if (/^[[{]/.test(value.trim())) {
      try {
        parsed = JSON.parse(value);
      } catch {
        parsed = value;
      }
    }
    cursor[path[path.length - 1]] = parsed;
  }
  return out;
}

function payloadToRows(text: string): Row[] {
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const rows: Row[] = [];
    const walk = (node: Record<string, unknown>, prefix: string) => {
      for (const [key, item] of Object.entries(node)) {
        const path = prefix ? `${prefix}.${key}` : key;
        if (item && typeof item === "object" && !Array.isArray(item)) {
          walk(item as Record<string, unknown>, path);
        } else {
          rows.push({
            key: path,
            value: typeof item === "string" ? item : JSON.stringify(item),
          });
        }
      }
    };
    walk(value, "");
    return rows;
  } catch {
    return [];
  }
}

/** Every reference this payload makes available, for pasting into a node. */
function referencesOf(text: string): string[] {
  const rows = payloadToRows(text);
  return rows.slice(0, 24).map((row) => `{{ input.${row.key} }}`);
}

export function TestRunPanel({
  value,
  onChange,
  onRun,
  onClose,
  triggerType,
  running,
}: {
  value: string;
  onChange: (next: string) => void;
  onRun: () => void;
  onClose: () => void;
  triggerType?: string;
  running?: boolean;
}) {
  const [mode, setMode] = useState<"fields" | "json">("fields");
  const presets = useMemo(() => presetsFor(triggerType), [triggerType]);
  const rows = useMemo(() => payloadToRows(value), [value]);

  const problem = useMemo(() => {
    if (!value.trim()) return null;
    try {
      const parsed = JSON.parse(value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return "The payload has to be a JSON object, so nodes can read named fields from it.";
      }
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : "That is not valid JSON.";
    }
  }, [value]);

  const references = useMemo(
    () => (problem ? [] : referencesOf(value)),
    [value, problem],
  );

  function setRows(next: Row[]) {
    onChange(JSON.stringify(rowsToPayload(next), null, 2));
  }

  return (
    <div className="surface absolute top-full right-0 z-30 mt-2 w-[30rem] max-w-[92vw] overflow-hidden rounded-3xl shadow-2xl shadow-black/50">
      <div className="border-b border-ink-800/70 px-5 py-4">
        <p className="text-sm font-semibold text-ink-100">
          Run with this input
        </p>
        <p className="mt-1 text-xs leading-relaxed text-ink-500">
          Sent as the trigger payload, and read by your nodes as{" "}
          <code className="rounded bg-ink-800/60 px-1 py-0.5 text-ink-300">
            {"{{ input.… }}"}
          </code>
          . Runs the latest saved version.
        </p>
      </div>

      <div className="space-y-4 px-5 py-4">
        <div>
          <p className="mb-2 text-xs font-medium tracking-wide text-ink-400 uppercase">
            Start from
          </p>
          <div className="flex flex-wrap gap-1.5">
            {presets.map((preset) => (
              <button
                key={preset.label}
                title={preset.hint}
                onClick={() =>
                  onChange(JSON.stringify(preset.payload, null, 2))
                }
                className="rounded-full border border-ink-700 px-3 py-1.5 text-xs text-ink-300 transition-colors hover:border-brand-400 hover:text-ink-100"
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex gap-1 rounded-full bg-ink-900/70 p-1">
          {(["fields", "json"] as const).map((option) => (
            <button
              key={option}
              onClick={() => setMode(option)}
              className={cx(
                "flex-1 rounded-full px-3 py-1.5 text-xs font-medium capitalize transition-colors",
                mode === option
                  ? "bg-ink-800 text-ink-100"
                  : "text-ink-400 hover:text-ink-200",
              )}
            >
              {option === "fields" ? "Fields" : "JSON"}
            </button>
          ))}
        </div>

        {mode === "fields" ? (
          <div className="space-y-2">
            {rows.length === 0 && (
              <p className="text-xs text-ink-500">
                No fields yet. Add one, or pick a starting point above.
              </p>
            )}
            {rows.map((row, index) => (
              <div key={index} className="flex items-center gap-2">
                <input
                  value={row.key}
                  onChange={(event) => {
                    const next = [...rows];
                    next[index] = { ...row, key: event.target.value };
                    setRows(next);
                  }}
                  placeholder="body.issue.number"
                  className="w-2/5 rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2 font-mono text-xs text-ink-100 outline-none focus:border-brand-400"
                />
                <input
                  value={row.value}
                  onChange={(event) => {
                    const next = [...rows];
                    next[index] = { ...row, value: event.target.value };
                    setRows(next);
                  }}
                  placeholder="value"
                  className="flex-1 rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2 text-[0.78rem] text-ink-100 outline-none focus:border-brand-400"
                />
                <button
                  onClick={() => setRows(rows.filter((_, i) => i !== index))}
                  aria-label={`Remove ${row.key || "field"}`}
                  className="rounded-lg p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
                >
                  <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
                    <path
                      d="M6 6l12 12M18 6L6 18"
                      stroke="currentColor"
                      strokeWidth="1.8"
                    />
                  </svg>
                </button>
              </div>
            ))}
            <button
              onClick={() => setRows([...rows, { key: "", value: "" }])}
              className="rounded-xl border border-dashed border-ink-700 px-3 py-2 text-xs text-ink-400 transition-colors hover:border-brand-400 hover:text-ink-100"
            >
              + Add a field
            </button>
          </div>
        ) : (
          <textarea
            value={value}
            onChange={(event) => onChange(event.target.value)}
            rows={9}
            spellCheck={false}
            placeholder='{"text": "hello"}'
            className="w-full resize-y rounded-2xl border border-ink-700 bg-ink-950/60 px-3.5 py-3 font-mono text-xs leading-relaxed text-ink-100 outline-none focus:border-brand-400"
          />
        )}

        {problem ? (
          <p
            className="rounded-xl border p-2.5 text-xs leading-relaxed"
            style={{
              borderColor:
                "color-mix(in oklab, var(--status-bad) 45%, transparent)",
              color: "var(--status-bad)",
            }}
          >
            {problem}
          </p>
        ) : (
          references.length > 0 && (
            <div>
              <p className="mb-1.5 text-xs font-medium tracking-wide text-ink-400 uppercase">
                Your nodes can read
              </p>
              <div className="flex flex-wrap gap-1.5">
                {references.map((reference) => (
                  <button
                    key={reference}
                    onClick={() =>
                      void navigator.clipboard?.writeText(reference)
                    }
                    title="Copy"
                    className="rounded-lg bg-ink-900/80 px-2 py-1 font-mono text-xs text-ink-300 transition-colors hover:text-ink-100"
                  >
                    {reference}
                  </button>
                ))}
              </div>
            </div>
          )
        )}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-ink-800/70 px-5 py-3.5">
        <span className="text-xs text-ink-500">
          {rows.length} field{rows.length === 1 ? "" : "s"}
        </span>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={onRun} disabled={!!problem} loading={running}>
            Run now
          </Button>
        </div>
      </div>
    </div>
  );
}
