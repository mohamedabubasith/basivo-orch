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
  "git.ticket": "var(--status-warn)",
  "git.autofix": "var(--status-good)",
  "git.comment": "var(--series)",
  "design.render": "var(--color-brand-300)",
  "social.post": "var(--color-accent-500)",
  "video.render": "var(--status-warn)",
};
export const DEFAULT_ACCENT = "var(--series)";

const GIT_TICKET = (
  <>
    <path d="M5 4.5h14v15l-3.5-2.5h-7L5 19.5v-15Z" />
    <path d="M9 9h6M9 12.5h4" />
  </>
);

const GIT_AUTOFIX = (
  <>
    <circle cx="6" cy="6" r="2.2" />
    <circle cx="6" cy="18" r="2.2" />
    <circle cx="18" cy="12" r="2.2" />
    <path d="M6 8.2v7.6M8 6.5c5 0 8 2 8 5.5" />
    <path d="M15.2 3.8l2 2-2 2" />
  </>
);

const GIT_COMMENT = (
  <>
    <path d="M4.5 6.2c0-.9.8-1.7 1.7-1.7h11.6c.9 0 1.7.8 1.7 1.7v8c0 .9-.8 1.7-1.7 1.7H10l-4 3.3v-3.3H6.2c-.9 0-1.7-.8-1.7-1.7v-8Z" />
    <path d="M8.5 9h7M8.5 12h4.5" />
  </>
);

const DESIGN_RENDER = (
  <>
    <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
    <path d="M3.5 15l4.2-4.2a1.6 1.6 0 0 1 2.3 0L14 14.7" />
    <path d="M14.4 13.2l1.5-1.5a1.6 1.6 0 0 1 2.3 0l2.3 2.3" />
    <circle cx="9" cy="9" r="1.3" />
  </>
);

const SOCIAL_POST = (
  <>
    <path d="M20.5 3.8 3.9 10.2c-.9.3-.9 1.6 0 1.9l6.3 2.1 2.1 6.3c.3.9 1.6.9 1.9 0L20.5 3.8Z" />
    <path d="M20.5 3.8 10.2 14.2" />
  </>
);

const VIDEO_RENDER = (
  <>
    <rect x="2.8" y="5.5" width="12.4" height="13" rx="2.4" />
    <path d="M15.2 10.4l4.1-2.6a.8.8 0 0 1 1.2.7v6.9a.8.8 0 0 1-1.2.7l-4.1-2.6" />
    <path d="M7.4 9.6v4.8l3.6-2.4-3.6-2.4Z" />
  </>
);

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
  "git.ticket": GIT_TICKET,
  "git.autofix": GIT_AUTOFIX,
  "git.comment": GIT_COMMENT,
  "design.render": DESIGN_RENDER,
  "social.post": SOCIAL_POST,
  "video.render": VIDEO_RENDER,
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

export function NodeIcon({
  type,
  className = "h-[18px] w-[18px]",
}: {
  type: string;
  className?: string;
}) {
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
export function NodeIconChip({
  type,
  size = 9,
}: {
  type: string;
  size?: 7 | 8 | 9;
}) {
  const accent = nodeAccent(type);
  const box =
    size === 7
      ? "h-7 w-7 rounded-lg"
      : size === 8
        ? "h-8 w-8 rounded-lg"
        : "h-9 w-9 rounded-xl";
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
