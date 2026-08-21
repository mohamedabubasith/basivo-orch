/**
 * The animated backdrop: beams of light travelling along circuit traces over
 * the drifting grid — the Aceternity/Magic-UI "background beams" pattern,
 * built on the same primitives those libraries use (SVG paths + CSS motion),
 * and thematically the right one for this product: data moving through
 * pipelines is literally what the app does.
 *
 * Colour appears only as the moving pulse, never as an ambient field — the
 * previous iterations proved both failure modes: static blobs read as lava
 * lamp, and no colour at all read as unfinished. A thin travelling gradient
 * is structured light, and each trace also exists as a faint static line so
 * the pulses visibly run *on* something.
 *
 * All decoration: aria-hidden, pointer-events none. The pulses are CSS
 * keyframes (stroke-dashoffset), so the global reduced-motion rule freezes
 * them into faint static traces.
 */

import { cx } from "../lib/cx";

/** Orthogonal traces, drawn for a 1440×900 stage that slices to fit. */
const TRACES = [
  "M-40 220 H 420 V 420 H 880 V 300 H 1480",
  "M-40 620 H 260 V 480 H 720 V 640 H 1480",
  "M420 -40 V 180 H 1020 V 520 H 1480",
  "M-40 760 H 560 V 860 H 1180 V 700 H 1480",
  "M1020 -40 V 120 H 620 V 320 H 160 V 560",
] as const;

/** Duration/delay pairs staggered so pulses never march in step. */
const TIMING = [
  { duration: "9s", delay: "0s" },
  { duration: "12s", delay: "-4s" },
  { duration: "10s", delay: "-7s" },
  { duration: "14s", delay: "-2s" },
  { duration: "11s", delay: "-9s" },
] as const;

export function Backdrop({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cx(
        "pointer-events-none absolute inset-0 overflow-hidden",
        className,
      )}
    >
      <div className="grid-bg animate-grid absolute inset-0 [mask-image:radial-gradient(ellipse_at_50%_-10%,black_25%,transparent_75%)]" />

      <svg
        className="absolute inset-0 h-full w-full"
        viewBox="0 0 1440 900"
        preserveAspectRatio="xMidYMid slice"
        fill="none"
      >
        <defs>
          <linearGradient id="beam-violet" x1="0" y1="0" x2="1" y2="0">
            <stop
              offset="0"
              stopColor="var(--color-brand-500)"
              stopOpacity="0"
            />
            <stop offset="0.5" stopColor="var(--color-brand-400)" />
            <stop
              offset="1"
              stopColor="var(--color-accent-500)"
              stopOpacity="0"
            />
          </linearGradient>
          <linearGradient id="beam-teal" x1="0" y1="0" x2="1" y2="0">
            <stop
              offset="0"
              stopColor="var(--color-accent-500)"
              stopOpacity="0"
            />
            <stop offset="0.5" stopColor="var(--color-accent-400)" />
            <stop
              offset="1"
              stopColor="var(--color-brand-400)"
              stopOpacity="0"
            />
          </linearGradient>
        </defs>

        {TRACES.map((trace, index) => (
          <g key={index}>
            {/* The trace itself: faint, static — the wire the pulse runs on. */}
            <path d={trace} stroke="var(--edge-strong)" strokeWidth="1" />
            {/* The pulse: a short lit segment walking the normalised path. */}
            <path
              d={trace}
              pathLength={1000}
              stroke={index % 2 === 0 ? "url(#beam-violet)" : "url(#beam-teal)"}
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeDasharray="90 910"
              style={{
                animation: `beam-travel ${TIMING[index].duration} linear infinite`,
                animationDelay: TIMING[index].delay,
              }}
            />
          </g>
        ))}
      </svg>
    </div>
  );
}
