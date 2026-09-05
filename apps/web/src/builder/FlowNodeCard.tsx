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
import { nodeSummary } from "./nodeSummary";

const STATUS = {
  running: { color: "var(--series)", label: "Running" },
  succeeded: { color: "var(--status-good)", label: "Succeeded" },
  failed: { color: "var(--status-bad)", label: "Failed" },
  skipped: { color: "var(--color-ink-400)", label: "Skipped" },
} as const;

/** Handle labels. The default port needs none — there is nothing to disambiguate. */
const PORT_LABEL: Record<string, string> = {
  out: "",
  true: "true",
  false: "false",
  handover: "hand over to another agent",
};

/**
 * The port that passes the whole conversation to a colleague agent. It hangs
 * off the bottom of the card, not the right side: the right side is where the
 * answer comes out, and a second right-hand dot labelled "hand over" read as a
 * second answer. Below the card, with its own label, it reads as what it is,
 * the agent stepping aside for another one.
 */
const HANDOVER = "handover";

/** Ports whose label and handle are tinted, so a branch reads at a glance. */
const PORT_TINT: Record<string, string> = {
  true: "var(--status-good)",
  false: "var(--status-warn)",
  handover: "var(--series)",
};

export function FlowNodeCard({ data, selected }: NodeProps<FlowNode>) {
  const status = data.runStatus ? STATUS[data.runStatus] : null;
  const allPorts = data.ports.length > 0 ? data.ports : ["out"];
  const ports = allPorts.filter((port) => port !== HANDOVER);
  const handsOver = allPorts.includes(HANDOVER);
  const accent = nodeAccent(data.nodeType);
  const summary = nodeSummary(data.nodeType, data.config ?? {});

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
        "group relative w-[236px] overflow-hidden rounded-xl border transition-colors duration-150",
        // Two shadows: a tight one for the edge and a wide soft one for lift.
        // A single flat border made these read as list rows rather than
        // objects sitting on a surface.
        "shadow-[0_1px_2px_rgba(0,0,0,0.10),0_12px_24px_-16px_rgba(0,0,0,0.45)]",
        data.runStatus === "running"
          ? // The glow is the border while it runs; a second one competes.
            "node-running border-transparent"
          : selected
            ? "border-brand-400 ring-2 ring-brand-400/25"
            : data.problem
              ? "border-[var(--status-bad)]"
              : "border-ink-600/70 hover:border-ink-500",
      )}
      style={{
        // The identity colour runs faintly through the whole card, not only
        // the icon chip — a flat `bg-ink-850` box with a coloured icon in the
        // corner read as plain no matter how good the icon was.
        // 5% of an accent over a near-white surface is invisible, which is
        // exactly how these looked in light mode: plain white boxes. The wash
        // is stronger and the identity now lives mostly in the rail below.
        background: `color-mix(in oklab, ${accent} 9%, var(--color-ink-850))`,
      }}
    >
      {/* While running: a light going round the edge, and an inner surface
          that covers everything but that edge. Rendered before the rail so the
          rail stays on top of it. */}
      {data.runStatus === "running" && (
        <>
          <span className="node-orbit" aria-hidden="true" />
          <span
            className="pointer-events-none absolute inset-[1.5px] rounded-[10px]"
            style={{
              background: `color-mix(in oklab, ${accent} 9%, var(--color-ink-850))`,
            }}
            aria-hidden="true"
          />
        </>
      )}

      {/* A full-height rail rather than a 1px top hairline. This is the thing
          you read at low zoom, when labels have stopped being legible — a
          canvas of a dozen nodes should sort into triggers / agents / devops
          by colour before you focus on any one of them. */}
      <span
        className="absolute inset-y-0 left-0 w-[3px]"
        style={{ background: accent }}
        aria-hidden="true"
      />

      {/* A trigger has no input: nothing can precede the thing that starts the
          flow, so there is no handle rather than a handle that rejects
          connections after the user has already dragged one to it. */}
      {!data.isTrigger && (
        <Handle
          type="target"
          position={Position.Left}
          // Above the header, which is a positioned sibling rendered after it
          // and was painting over the handle's inner half: a drop that landed
          // there hit the header, not the port. Found by the QA plugin.
          className="!z-10 !h-3.5 !w-3.5 !border-2 !border-[var(--color-ink-900)] !bg-ink-400 transition-all hover:!scale-125 hover:!bg-brand-400"
        />
      )}

      <div className="relative flex items-start gap-2.5 py-2.5 pr-3.5 pl-3">
        <NodeIconChip type={data.nodeType} size={8} />

        <div className="min-w-0 flex-1">
          <p className="truncate text-[0.82rem] leading-tight font-medium text-ink-100">
            {data.label}
          </p>
          {/* What this node will actually do — which model, which repository,
              which channel. Without it a canvas of four agents is four
              identical cards and every question means opening one. */}
          <p
            className="mt-1 truncate text-xs leading-tight text-ink-400"
            title={summary}
          >
            {summary || data.nodeType}
          </p>
        </div>
      </div>

      {(status || data.problem) && (
        <div className="relative border-t border-ink-700/60 px-3.5 py-2">
          {status && (
            <p
              className="flex items-center gap-1.5 text-xs"
              style={{ color: status.color }}
            >
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
                <span className="ml-auto truncate font-mono text-ink-400">
                  {data.runDetail}
                </span>
              )}
            </p>
          )}

          {data.problem && (
            <p
              className="mt-1 line-clamp-2 text-xs leading-snug"
              style={{ color: "var(--status-bad)" }}
              title={data.problem}
            >
              {data.problem.startsWith(data.label + ": ")
                ? data.problem.slice(data.label.length + 2)
                : data.problem}
            </p>
          )}
        </div>
      )}

      {handsOver && (
        <div className="relative mt-0.5 flex flex-col items-center pb-2">
          <span
            className="text-[0.7rem] tracking-wide"
            style={{ color: PORT_TINT[HANDOVER] }}
          >
            {PORT_LABEL[HANDOVER]}
          </span>
          <Handle
            id={HANDOVER}
            type="source"
            position={Position.Bottom}
            className="!h-3.5 !w-3.5 !border-2 !border-[var(--color-ink-900)] transition-transform hover:!scale-125"
            style={{
              position: "absolute",
              bottom: -1,
              left: "50%",
              transform: "translateX(-50%)",
              background: PORT_TINT[HANDOVER],
            }}
          />
        </div>
      )}

      {/* Ports are labelled when there is more than one, because "which branch
          is this?" is not answerable from geometry alone. */}
      <div className="absolute top-0 -right-px flex h-full flex-col justify-center gap-3 pr-0">
        {ports.map((port) => (
          <div key={port} className="relative flex h-3 items-center">
            {ports.length > 1 && (
              <span
                className="absolute right-4 text-[0.7rem] whitespace-nowrap"
                style={{ color: PORT_TINT[port] ?? "var(--status-good)" }}
              >
                {PORT_LABEL[port] ?? port}
              </span>
            )}
            <Handle
              id={port}
              type="source"
              position={Position.Right}
              className="!h-3.5 !w-3.5 !border-2 !border-[var(--color-ink-900)] transition-transform hover:!scale-125"
              style={{
                position: "relative",
                right: 0,
                top: 0,
                transform: "none",
                background: PORT_TINT[port] ?? "var(--color-brand-400)",
              }}
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
        <path
          d="M2.5 6h7"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}
