/** Small pieces shared by the app pages. Kept here so three files do not each
 *  grow their own slightly different page header and timestamp. */

import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  action,
}: {
  /** Tiny uppercase kicker above the title — situates the page in one glance. */
  eyebrow?: string;
  title: string;
  subtitle?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && (
          <p className="mb-1.5 text-[0.68rem] font-medium tracking-[0.14em] text-brand-400 uppercase">
            {eyebrow}
          </p>
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-ink-100">{title}</h1>
        {subtitle && <p className="mt-1.5 max-w-2xl leading-relaxed text-ink-400">{subtitle}</p>}
      </div>
      {action}
    </header>
  );
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.348],
  ["month", 12],
  ["year", Infinity],
];

/**
 * "3 minutes ago", with the exact timestamp on hover.
 *
 * Relative reads faster for recent things, which is nearly all of them here,
 * but it is useless for correlating against a log — hence the `title`, which
 * keeps the precise value one hover away rather than gone.
 */
export function RelativeTime({ value }: { value: string | null }) {
  if (!value) return <span className="text-ink-600">never</span>;

  const date = new Date(value);
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let delta = (date.getTime() - Date.now()) / 1000;

  for (const [unit, size] of UNITS) {
    if (Math.abs(delta) < size) {
      return <time title={date.toLocaleString()}>{format.format(Math.round(delta), unit)}</time>;
    }
    delta /= size;
  }
  return <time title={date.toLocaleString()}>{date.toLocaleDateString()}</time>;
}

/** Milliseconds as something a person reads without counting digits. */
export function duration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}
