/**
 * One icon and one accent per node type, shared by everything that shows a
 * node: the canvas card, the palette, and any list that names a node type.
 *
 * Extracted from FlowNodeCard the day the palette shipped without icons — two
 * copies of this table would disagree within a week, and a node type whose
 * palette entry and canvas card look unrelated is worse than either alone.
 */

import type { ReactNode } from "react";

export const NODE_ACCENT: Record<string, string> = {
  "trigger.manual": "var(--color-accent-500)",
  "trigger.webhook": "var(--color-accent-500)",
  "trigger.schedule": "var(--color-accent-500)",
  "http.request": "var(--series)",
  "logic.condition": "var(--status-warn)",
  "data.set": "var(--status-good)",
  "agent.llm": "var(--color-brand-300)",
  "code.python": "var(--color-brand-300)",
};
export const DEFAULT_ACCENT = "var(--series)";

const PATHS: Record<string, ReactNode> = {
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
  "agent.llm": (
    <>
      <rect x="7" y="7" width="10" height="10" rx="2.5" />
      <path d="M12 2.5v2.3M12 19.2v2.3M2.5 12h2.3M19.2 12h2.3M5 5l1.6 1.6M17.4 17.4 19 19M19 5l-1.6 1.6M6.6 17.4 5 19" />
    </>
  ),
  "code.python": (
    <>
      <path d="M8.5 8 5 12l3.5 4M15.5 8 19 12l-3.5 4" />
      <path d="M13.2 6.5 10.8 17.5" />
    </>
  ),
};

const FALLBACK = (
  <>
    <rect x="4.5" y="4.5" width="15" height="15" rx="4" />
    <path d="M9 12h6" />
  </>
);

export function nodeAccent(type: string): string {
  return NODE_ACCENT[type] ?? DEFAULT_ACCENT;
}

export function NodeIcon({ type, className = "h-[18px] w-[18px]" }: { type: string; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[type] ?? FALLBACK}
    </svg>
  );
}

/** The tinted square the icon sits in — the same chip on card and palette. */
export function NodeIconChip({ type, size = 9 }: { type: string; size?: 7 | 8 | 9 }) {
  const accent = nodeAccent(type);
  const box = size === 7 ? "h-7 w-7 rounded-lg" : size === 8 ? "h-8 w-8 rounded-lg" : "h-9 w-9 rounded-xl";
  const glyph = size === 7 ? "h-3.5 w-3.5" : "h-[18px] w-[18px]";
  return (
    <span
      className={`grid flex-none place-items-center ${box}`}
      style={{
        background: `color-mix(in oklab, ${accent} 18%, transparent)`,
        color: accent,
        boxShadow: `0 0 0 1px color-mix(in oklab, ${accent} 22%, transparent)`,
      }}
      aria-hidden="true"
    >
      <NodeIcon type={type} className={glyph} />
    </span>
  );
}
