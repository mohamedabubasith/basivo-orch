/**
 * One node on the canvas.
 *
 * It carries three layers of state that have to stay distinguishable at a
 * glance: what the node *is* (an icon, a name, and now a per-type accent
 * running through icon, hairline and card wash together, so a glance across a
 * busy canvas reads "trigger / call / decision / AI" before any label is
 * legible), whether it is *valid* (a problem from the last validate), and what
 * it *did* (the last run). Those are different questions, so they get
 * different channels — a ring for validity, a labelled footer for run status —
 * and a red node is never ambiguous between "misconfigured" and "failed at
 * 3am".
 *
 * Run status never travels as colour alone: every state carries an icon and a
 * word. The colours are theme variables rather than literals so the card is
 * legible on a white canvas too.
 */

import { motion } from "motion/react";
import { Handle, Position, type NodeProps } from "@xyflow/react";

import { cx } from "../lib/cx";
import type { FlowNode } from "./graph";

const STATUS = {
  running: { color: "var(--series)", label: "Running" },
  succeeded: { color: "var(--status-good)", label: "Succeeded" },
  failed: { color: "var(--status-bad)", label: "Failed" },
  skipped: { color: "var(--color-ink-400)", label: "Skipped" },
} as const;

/** Handle labels. The default port needs none — there is nothing to disambiguate. */
const PORT_LABEL: Record<string, string> = { out: "", true: "true", false: "false" };

/**
 * One accent per node type, not just "trigger or not". A canvas with a dozen
 * nodes is scanned by colour long before anyone reads a label — a decision
 * point, an AI call and a plain HTTP request should not all be the same hue.
 */
const ACCENT: Record<string, string> = {
  "trigger.manual": "var(--color-accent-500)",
  "trigger.webhook": "var(--color-accent-500)",
  "trigger.schedule": "var(--color-accent-500)",
  "http.request": "var(--series)",
  "logic.condition": "var(--status-warn)",
  "data.set": "var(--status-good)",
  "agent.llm": "var(--color-brand-300)",
};
const DEFAULT_ACCENT = "var(--series)";

const ICONS: Record<string, React.ReactNode> = {
  "trigger.manual": <path d="M8 5.5v13l11-6.5-11-6.5Z" />,
  "trigger.webhook": (
    <>
      <path d="M9 12a3 3 0 1 1 4.2 2.75" />
      <path d="M13 19h4.5a3 3 0 0 0 0-6h-.6" />
      <path d="M10.4 19H6.5a3 3 0 0 1-1.6-5.5" />
    </>
  ),
  "trigger.schedule": (
    <>
      <circle cx="12" cy="12" r="7.5" />
      <path d="M12 7.8V12l2.8 1.8" />
    </>
  ),
  "http.request": (
    <>
      <circle cx="12" cy="12" r="7.5" />
      <path d="M4.5 12h15M12 4.5c2 2.4 2 12.6 0 15M12 4.5c-2 2.4-2 12.6 0 15" />
    </>
  ),
  "logic.condition": (
    <>
      <path d="M5 6h3l4 6 4-6h3" />
      <path d="M5 18h3l3-4.5" />
    </>
  ),
  "data.set": (
    <>
      <ellipse cx="12" cy="6.8" rx="7" ry="2.8" />
      <path d="M5 6.8v10.4c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8V6.8" />
      <path d="M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8" />
    </>
  ),
  // A chip with radiating points — reads as "a model", distinct from the
  // plain rounded-square every other node fell back to before this existed.
  "agent.llm": (
    <>
      <rect x="7" y="7" width="10" height="10" rx="2.5" />
      <path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5 5l1.6 1.6M17.4 17.4 19 19M19 5l-1.6 1.6M6.6 17.4 5 19" />
    </>
  ),
};

const FALLBACK_ICON = (
  <>
    <rect x="4.5" y="4.5" width="15" height="15" rx="4" />
    <path d="M9 12h6" />
  </>
);

export function FlowNodeCard({ data, selected }: NodeProps<FlowNode>) {
  const status = data.runStatus ? STATUS[data.runStatus] : null;
  const ports = data.ports.length > 0 ? data.ports : ["out"];
  const accent = ACCENT[data.nodeType] ?? DEFAULT_ACCENT;

  return (
    <motion.div
      // Plays once, on mount only — a node just dropped onto the canvas
      // arrives with a little life in it, but a node merely re-rendering
      // because its run status changed does not replay this and jump.
      initial={{ opacity: 0, scale: 0.86 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: "spring", stiffness: 420, damping: 28 }}
      whileHover={{ y: -2 }}
      className={cx(
        "group relative w-[248px] rounded-2xl border transition-colors duration-150",
        "shadow-[0_1px_2px_rgba(0,0,0,0.16),0_10px_28px_-14px_rgba(0,0,0,0.55)]",
        selected
          ? "border-brand-400 ring-2 ring-brand-400/25"
          : data.problem
            ? "border-[var(--status-bad)]"
            : "border-ink-600/70 hover:border-ink-500",
      )}
      style={{
        // The identity colour runs faintly through the whole card, not only
        // the icon chip — a flat `bg-ink-850` box with a coloured icon in the
        // corner read as plain no matter how good the icon was.
        background: `color-mix(in oklab, ${accent} 5%, var(--color-ink-850))`,
      }}
    >
      {/* A hairline in the node's accent. Identity you can read at low zoom,
          when the label has stopped being legible. */}
      <span
        className="absolute inset-x-3 top-0 h-px rounded-full"
        style={{ background: accent, opacity: 0.85 }}
        aria-hidden="true"
      />

      {/* A trigger has no input: nothing can precede the thing that starts the
          flow, so there is no handle rather than a handle that rejects
          connections after the user has already dragged one to it. */}
      {!data.isTrigger && (
        <Handle
          type="target"
          position={Position.Left}
          className="!h-3 !w-3 !border-2 !border-ink-950 !bg-ink-400 transition-colors hover:!bg-brand-400"
        />
      )}

      <div className="flex items-center gap-3 px-3.5 py-3">
        <span
          className="grid h-9 w-9 flex-none place-items-center rounded-xl ring-1 ring-inset"
          style={{
            background: `color-mix(in oklab, ${accent} 18%, transparent)`,
            color: accent,
            boxShadow: `0 0 0 1px color-mix(in oklab, ${accent} 22%, transparent)`,
          }}
          aria-hidden="true"
        >
          <svg
            viewBox="0 0 24 24"
            className="h-[18px] w-[18px]"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            {ICONS[data.nodeType] ?? FALLBACK_ICON}
          </svg>
        </span>

        <div className="min-w-0 flex-1">
          <p className="truncate text-[0.82rem] leading-tight font-medium text-ink-100">
            {data.label}
          </p>
          <p className="mt-0.5 truncate font-mono text-[0.65rem] text-ink-500">{data.nodeType}</p>
        </div>
      </div>

      {(status || data.problem) && (
        <div className="border-t border-ink-700/60 px-3.5 py-2">
          {status && (
            <p className="flex items-center gap-1.5 text-[0.68rem]" style={{ color: status.color }}>
              {data.runStatus === "running" ? (
                <span className="relative flex h-2 w-2">
                  <span
                    className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-70"
                    style={{ backgroundColor: status.color }}
                  />
                  <span
                    className="relative inline-flex h-2 w-2 rounded-full"
                    style={{ backgroundColor: status.color }}
                  />
                </span>
              ) : (
                <StatusGlyph state={data.runStatus!} />
              )}
              <span className="font-medium">{status.label}</span>
              {data.runDetail && (
                <span className="ml-auto truncate font-mono text-ink-400">{data.runDetail}</span>
              )}
            </p>
          )}

          {data.problem && (
            <p
              className="mt-1 line-clamp-2 text-[0.68rem] leading-snug"
              style={{ color: "var(--status-bad)" }}
              title={data.problem}
            >
              {data.problem.replace(/^Node '[^']+' \([^)]*\) (is )?/, "")}
            </p>
          )}
        </div>
      )}

      {/* Ports are labelled when there is more than one, because "which branch
          is this?" is not answerable from geometry alone. */}
      <div className="absolute top-0 -right-px flex h-full flex-col justify-center gap-3 pr-0">
        {ports.map((port) => (
          <div key={port} className="relative flex h-3 items-center">
            {ports.length > 1 && (
              <span
                className="absolute right-4 text-[0.62rem] whitespace-nowrap"
                style={{ color: port === "false" ? "var(--status-warn)" : "var(--status-good)" }}
              >
                {PORT_LABEL[port] ?? port}
              </span>
            )}
            <Handle
              id={port}
              type="source"
              position={Position.Right}
              style={{ position: "relative", right: 0, top: 0, transform: "none" }}
              className={cx(
                "!h-3 !w-3 !border-2 !border-ink-950 transition-transform hover:!scale-125",
                port === "false"
                  ? "!bg-[var(--status-warn)]"
                  : port === "true"
                    ? "!bg-[var(--status-good)]"
                    : "!bg-brand-400",
              )}
            />
          </div>
        ))}
      </div>
    </motion.div>
  );
}

function StatusGlyph({ state }: { state: keyof typeof STATUS }) {
  return (
    <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none" aria-hidden="true">
      {state === "succeeded" ? (
        <path
          d="M2.5 6.4 4.8 8.7 9.5 3.9"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : state === "failed" ? (
        <path
          d="M3.6 3.6l4.8 4.8M8.4 3.6l-4.8 4.8"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      ) : (
        <path d="M2.5 6h7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      )}
    </svg>
  );
}
