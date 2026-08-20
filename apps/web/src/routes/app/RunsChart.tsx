/**
 * Runs per day, succeeded against failed.
 *
 * The dashboard had a 1d / 7d / 30d control and nothing that changed shape
 * over time: the numbers moved, but a trend — three quiet days then a spike of
 * failures — was invisible. That is the one question a window control exists
 * to answer.
 *
 * **Bars, not a sparkline.** These are discrete daily counts, and the reading
 * is "how many, and how many broke". An area chart would imply a continuous
 * quantity and hide the second half of that question.
 *
 * **Stacked with status colours, which is the one legitimate use of them.**
 * Succeeded and failed are states, not series, so they wear the reserved
 * status tokens and are labelled in a legend — never colour alone. A 2px gap
 * separates the two segments so the boundary survives at small sizes.
 */

import { useState } from "react";

interface Day {
  date: string;
  succeeded: number;
  failed: number;
  other: number;
}

const BAR_RADIUS = 4;

export function RunsChart({ daily }: { daily: Day[] }) {
  const [hover, setHover] = useState<number | null>(null);

  const peak = Math.max(1, ...daily.map((day) => day.succeeded + day.failed + day.other));
  const busiest = daily.reduce((sum, day) => sum + day.succeeded + day.failed + day.other, 0);

  if (!daily.length) return null;

  return (
    <div>
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <div>
          <h3 className="text-base font-semibold text-ink-100">Runs per day</h3>
          <p className="mt-1 text-sm text-ink-400">
            {busiest === 0
              ? "Nothing has run in this window."
              : "Where the failures cluster is usually a date, not a node."}
          </p>
        </div>
        {/* A legend for two series, always — identity is never colour alone. */}
        <div className="flex flex-none items-center gap-3 text-[0.7rem] text-ink-400">
          <span className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-sm"
              style={{ background: "var(--status-good)" }}
              aria-hidden="true"
            />
            Succeeded
          </span>
          <span className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-sm"
              style={{ background: "var(--status-bad)" }}
              aria-hidden="true"
            />
            Failed
          </span>
        </div>
      </div>

      <div className="relative flex h-40 items-end gap-[3px]">
        {daily.map((day, index) => {
          const total = day.succeeded + day.failed + day.other;
          const height = total === 0 ? 2 : Math.max(6, (total / peak) * 148);
          const failedShare = total ? day.failed / total : 0;
          const active = hover === index;

          return (
            <div
              key={day.date}
              // Capped width: a seven-day window with two busy days otherwise
              // produces 150px slabs that read as blocks rather than bars.
              className="group relative flex h-full flex-1 cursor-default items-end justify-center"
              // The hit target is the whole column, not the bar: a 6px bar on
              // a quiet day is otherwise unhoverable.
              onMouseEnter={() => setHover(index)}
              onMouseLeave={() => setHover(null)}
            >
              {total === 0 ? (
                // A quiet day is a small centred tick, not a full-width rule:
                // a 2px bar stretched across the column reads as an axis line
                // and invents structure that is not there.
                <span
                  className="mb-[1px] h-[3px] w-3 rounded-full"
                  style={{ background: "var(--edge-strong)" }}
                  aria-hidden="true"
                />
              ) : (
              <div
                className="flex w-full max-w-[54px] flex-col justify-end overflow-hidden transition-opacity"
                style={{
                  height,
                  borderRadius: BAR_RADIUS,
                  opacity: hover === null || active ? 1 : 0.45,
                }}
              >
                {day.failed > 0 && (
                  <span
                    className="w-full flex-none"
                    style={{
                      height: `${failedShare * 100}%`,
                      background: "var(--status-bad)",
                      // The 2px surface gap between stacked segments, so the
                      // boundary reads at any size.
                      marginBottom: day.succeeded + day.other > 0 ? 2 : 0,
                      borderRadius: `${BAR_RADIUS}px ${BAR_RADIUS}px 0 0`,
                    }}
                  />
                )}
                {day.succeeded + day.other > 0 && (
                  <span
                    className="w-full flex-1"
                    style={{
                      background:
                        day.other > 0 && day.succeeded === 0
                          ? "var(--series)"
                          : "var(--status-good)",
                      borderRadius:
                        day.failed > 0 ? `0 0 ${BAR_RADIUS}px ${BAR_RADIUS}px` : BAR_RADIUS,
                    }}
                  />
                )}
              </div>
              )}

              {active && total > 0 && (
                <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 -translate-x-1/2 rounded-lg border border-ink-700 bg-ink-900 px-2.5 py-1.5 whitespace-nowrap shadow-lg">
                  <p className="text-[0.68rem] font-medium text-ink-100">
                    {new Date(day.date).toLocaleDateString(undefined, {
                      day: "numeric",
                      month: "short",
                    })}
                  </p>
                  <p className="mt-0.5 text-[0.66rem] text-ink-400">
                    {day.succeeded} succeeded
                    {day.failed > 0 && ` · ${day.failed} failed`}
                    {day.other > 0 && ` · ${day.other} in flight`}
                  </p>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Only the ends are labelled. A tick under every one of thirty bars is
          noise, and the tooltip carries the exact date. */}
      <div className="mt-2 flex justify-between text-[0.66rem] text-ink-500">
        <span>{formatDay(daily[0].date)}</span>
        <span>{formatDay(daily[daily.length - 1].date)}</span>
      </div>
    </div>
  );
}

function formatDay(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
