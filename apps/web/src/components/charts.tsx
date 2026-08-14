/**
 * Chart primitives, built to fixed mark specs: bars well under the 24px cap
 * with a rounded data-end and a square baseline, a recessive track one step off
 * the surface, and labels on every row because there are few of them.
 *
 * Colour lives in `lib/viz.ts` — see the note there for why it is one hue per
 * chart rather than a ramp.
 */

import { motion, useReducedMotion } from "motion/react";
import { useState, type ReactNode } from "react";

import { cx } from "../lib/cx";
import { SERIES, SERIES_DIM, STATUS, type StatusTone } from "../lib/viz";

/* ----------------------------------------------------------- stat tile --- */

export function StatTile({
  label,
  value,
  hint,
  tone,
  icon,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: StatusTone;
  icon?: ReactNode;
}) {
  return (
    <div className="surface relative overflow-hidden rounded-xl p-5">
      {/* The tile's one allowed flourish: a hairline in the metric's colour. */}
      <span
        aria-hidden="true"
        className="absolute inset-x-4 top-0 h-px"
        style={{ background: tone ? STATUS[tone] : "var(--series)", opacity: 0.7 }}
      />
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-ink-400">{label}</p>
        {icon}
      </div>
      {/* A single number is a stat tile, never a one-bar bar chart. Tabular
          numerals so a row of tiles reads as one aligned instrument panel. */}
      <p
        className="mt-2 text-[2rem] leading-none font-semibold tracking-tight text-ink-100 [font-variant-numeric:tabular-nums]"
        style={tone ? { color: STATUS[tone] } : undefined}
      >
        {value}
      </p>
      {hint && <p className="mt-2 text-xs leading-relaxed text-ink-500">{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------ horizontal bars --- */

export interface BarDatum {
  key: string;
  label: string;
  value: number;
  /** Shown at the bar's end. The chart labels every row because there are few. */
  display: string;
  /** Secondary line under the label. */
  meta?: string;
  /** Marks this row as the story; it takes the accent, the rest recede. */
  emphasis?: boolean;
  tone?: StatusTone;
}

export function BarList({
  data,
  caption,
  emptyLabel = "No data yet",
}: {
  data: BarDatum[];
  caption?: string;
  emptyLabel?: string;
}) {
  const reduceMotion = useReducedMotion();
  const [hovered, setHovered] = useState<string | null>(null);
  const max = Math.max(...data.map((d) => d.value), 0.0001);
  const anyEmphasis = data.some((d) => d.emphasis);

  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-ink-500">{emptyLabel}</p>;
  }

  return (
    <div>
      <ul className="space-y-3">
        {data.map((datum, index) => {
          // One hue for every bar; the accent only when a row is the story.
          const colour = datum.tone
            ? STATUS[datum.tone]
            : anyEmphasis && !datum.emphasis
              ? SERIES_DIM
              : SERIES;
          return (
            <li
              key={datum.key}
              onMouseEnter={() => setHovered(datum.key)}
              onMouseLeave={() => setHovered(null)}
              className="group"
            >
              <div className="mb-1.5 flex items-baseline justify-between gap-3">
                <span className="truncate text-sm text-ink-200">{datum.label}</span>
                <span className="flex-none font-mono text-xs text-ink-300">{datum.display}</span>
              </div>

              {/* Track is one step off the surface, hairline-quiet. The bar is
                  8px — well under the 24px cap — with a rounded data-end and a
                  square baseline. */}
              <div className="relative h-2 w-full overflow-hidden rounded-l-[2px] rounded-r-[4px] bg-ink-800/70">
                <motion.div
                  className="absolute inset-y-0 left-0 rounded-l-[2px] rounded-r-[4px]"
                  style={{ backgroundColor: colour }}
                  initial={reduceMotion ? false : { width: 0 }}
                  animate={{ width: `${Math.max(1.5, (datum.value / max) * 100)}%` }}
                  transition={{ duration: 0.6, delay: index * 0.04, ease: [0.21, 0.5, 0.35, 1] }}
                />
              </div>

              {(datum.meta || hovered === datum.key) && (
                <p className="mt-1 text-xs text-ink-500">{datum.meta}</p>
              )}
            </li>
          );
        })}
      </ul>
      {caption && <p className="mt-4 text-xs leading-relaxed text-ink-600">{caption}</p>}
    </div>
  );
}

/* ---------------------------------------------------------------- panel --- */

export function Panel({
  title,
  description,
  action,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("surface rounded-xl p-6", className)}>
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-ink-100">{title}</h2>
          {description && (
            <p className="mt-1 text-sm leading-relaxed text-ink-400">{description}</p>
          )}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

/* ----------------------------------------------------------- status pip --- */

/** Status never travels as colour alone — it always carries a label. */
export function StatusPip({ tone, children }: { tone: StatusTone; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: STATUS[tone] }}>
      <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none" aria-hidden="true">
        {tone === "good" ? (
          <path
            d="M2.5 6.4 4.8 8.7 9.5 3.9"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ) : tone === "warn" ? (
          <path
            d="M6 2.5v4M6 8.8v.4"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
        ) : (
          <path
            d="M3.6 3.6l4.8 4.8M8.4 3.6l-4.8 4.8"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
        )}
      </svg>
      {children}
    </span>
  );
}
