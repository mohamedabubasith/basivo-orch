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
import { NodeIconChip, nodeAccent } from "./nodeIcons";

const STATUS = {
  running: { color: "var(--series)", label: "Running" },
  succeeded: { color: "var(--status-good)", label: "Succeeded" },
  failed: { color: "var(--status-bad)", label: "Failed" },
  skipped: { color: "var(--color-ink-400)", label: "Skipped" },
} as const;

/** Handle labels. The default port needs none — there is nothing to disambiguate. */
const PORT_LABEL: Record<string, string> = { out: "", true: "true", false: "false" };

export function FlowNodeCard({ data, selected }: NodeProps<FlowNode>) {
  const status = data.runStatus ? STATUS[data.runStatus] : null;
  const ports = data.ports.length > 0 ? data.ports : ["out"];
  const accent = nodeAccent(data.nodeType);

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
        <NodeIconChip type={data.nodeType} />

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
