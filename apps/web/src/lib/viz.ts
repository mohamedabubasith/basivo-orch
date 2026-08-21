/**
 * Chart colour and number formatting.
 *
 * Separated from the components so the palette has one home and fast refresh
 * keeps working — a module that exports both components and constants loses it.
 *
 * Both sets were validated against the dark chart surface (#0d1018) rather than
 * chosen by eye:
 *
 * - `SERIES` is a single hue. Node names are nominal, so shading bars
 *   darker-where-bigger would double-encode the bar length and burn the only
 *   free channel on information the chart already shows.
 * - `STATUS` sits in the 0.48–0.67 OKLCH lightness band for a dark surface and
 *   clears 3:1 against it. Its worst colourblind separation is ΔE 7.9, which is
 *   inside the band that is legal *only* with secondary encoding — which is why
 *   `StatusPip` always draws an icon and a label, never colour alone.
 */

//: Single series hue.
export const SERIES = "var(--series)";
//: The recessive step, for rows that are context rather than the story.
export const SERIES_DIM = "var(--series-dim)";

//: Reserved status palette. Never reused for a non-status series.
export const STATUS = {
  good: "var(--status-good)",
  warn: "var(--status-warn)",
  bad: "var(--status-bad)",
} as const;

export type StatusTone = keyof typeof STATUS;

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "\u2014";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatPercent(
  value: number | null | undefined,
  digits = 0,
): string {
  if (value === null || value === undefined) return "\u2014";
  return `${(value * 100).toFixed(digits)}%`;
}
