/**
 * The how-it-works picture: the product's own pipeline, playing on a loop.
 *
 * This replaced three screenshots. A screenshot of a builder is a picture of
 * something that moves, and it aged the moment the canvas was restyled; the
 * cards, the icon chips, the dashed edge and the port dots here are the ones
 * the real canvas draws (`builder/FlowNodeCard.tsx`, `builder/FlowEdge.tsx`,
 * `builder/nodeIcons.tsx`), so the page cannot drift from the product without
 * someone noticing.
 *
 * The numbers are one real run against a real repository: issue #8 in, pull
 * request #9 out, 36 seconds, nine cents, 12 tokens in and 1,894 out. Nothing
 * here is a rounded-up illustration.
 *
 * Every beat is derived from one integer, so there is exactly one timer to
 * clear on unmount, and every line the animation can ever show is in the DOM
 * from the first frame — a footer that grows a row when a node finishes would
 * shove the rest of the page down mid-scroll.
 */

import { motion, useReducedMotion } from "motion/react";
import { useEffect, useState } from "react";

import { NodeIcon, NodeIconChip, nodeAccent } from "../../builder/nodeIcons";

/** Tokens the agent really returned. */
const OUT_TOKENS = 1894;

/**
 * How long each beat holds, in milliseconds. The index is the whole state of
 * the animation:
 *
 *   0 nothing has happened yet   4 the branch resolves, the second edge draws
 *   1 the webhook fires          5 the agent works, tokens climbing
 *   2 issue #8, the first edge   6 the agent settles: 36 seconds, nine cents
 *   3 the condition evaluates    7 the pull request, then hold and repeat
 */
const BEAT_MS = [700, 850, 900, 850, 900, 2600, 1200, 3000];
const FINAL = BEAT_MS.length - 1;

const STATUS = {
  running: { color: "var(--series)", label: "Running" },
  succeeded: { color: "var(--status-good)", label: "Succeeded" },
} as const;

type Status = keyof typeof STATUS | null;

export function FlowAnimation() {
  // Reduced motion is not a slower animation, it is no animation: the finished
  // run, drawn once, with no timer ever started.
  const still = useReducedMotion() ?? false;
  const [beat, setBeat] = useState(still ? FINAL : 0);
  const [ticked, setTicked] = useState(0);

  useEffect(() => {
    if (still) return;
    const timer = setTimeout(
      () => setBeat((b) => (b + 1) % BEAT_MS.length),
      BEAT_MS[beat],
    );
    return () => clearTimeout(timer);
  }, [beat, still]);

  useEffect(() => {
    if (still || beat !== 5) return;
    let n = 0;
    const id = setInterval(() => {
      n = Math.min(OUT_TOKENS, n + 137);
      setTicked(n);
    }, 120);
    // Reset on the way out, not on the way in: the loop comes back to this
    // beat, and a count that starts at last time's total reads as a glitch.
    return () => {
      clearInterval(id);
      setTicked(0);
    };
  }, [beat, still]);

  const trigger: Status = beat >= 2 ? "succeeded" : beat === 1 ? "running" : null;
  const condition: Status = beat >= 4 ? "succeeded" : beat === 3 ? "running" : null;
  const agent: Status = beat >= 6 ? "succeeded" : beat === 5 ? "running" : null;
  const out = beat >= 6 ? OUT_TOKENS : beat === 5 ? ticked : 0;

  return (
    <div className="surface rounded-2xl px-5 py-8 sm:px-8">
      <div className="flex flex-col items-center md:flex-row md:justify-center">
        <Card
          type="trigger.webhook"
          name="GitHub webhook"
          subtitle="issues.opened"
          status={trigger}
          detail={trigger === "succeeded" ? "12ms" : ""}
          meta={beat >= 2 ? "issue #8 opened" : ""}
          isTrigger
          still={still}
        />
        <Connector drawn={beat >= 2} still={still} />
        <Card
          type="logic.condition"
          name="Is it a bug?"
          subtitle="labels contains bug"
          status={condition}
          detail={condition === "succeeded" ? "true" : ""}
          detailTone={condition === "succeeded" ? "var(--status-good)" : undefined}
          meta={beat >= 4 ? "false branch skipped" : ""}
          portTone="var(--status-good)"
          still={still}
        />
        <Connector drawn={beat >= 4} still={still} />
        <Card
          type="agent.llm"
          name="Repair agent"
          subtitle="reads the repository, writes the fix"
          status={agent}
          detail={agent === "succeeded" ? "36s · $0.09" : ""}
          meta={
            beat >= 5 ? `12 in · ${out.toLocaleString("en-US")} out tokens` : ""
          }
          isLast
          still={still}
        />
      </div>

      {/* The row is always this tall, so the chip arriving moves nothing. */}
      <div className="mt-7 flex h-8 items-center justify-center">
        <motion.span
          className="inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs"
          style={{
            borderColor: "color-mix(in oklab, var(--status-good) 38%, transparent)",
            background: "color-mix(in oklab, var(--status-good) 12%, transparent)",
            color: "var(--status-good)",
          }}
          initial={still ? false : { opacity: 0, y: 4 }}
          animate={{ opacity: beat >= 7 ? 1 : 0, y: beat >= 7 ? 0 : 4 }}
          transition={{ duration: still ? 0 : 0.35, ease: "easeOut" }}
        >
          <NodeIcon type="git.autofix" className="h-3.5 w-3.5" />
          Pull request #9 opened
        </motion.span>
      </div>

      <p className="mt-5 text-center text-xs text-ink-500">
        One real run, replayed. The duration, the cost and the token counts are
        the ones that run recorded.
      </p>
    </div>
  );
}

/* --------------------------------------------------------------- card --- */

function Card({
  type,
  name,
  subtitle,
  status,
  detail,
  detailTone,
  meta,
  isTrigger = false,
  isLast = false,
  portTone,
  still,
}: {
  type: string;
  name: string;
  subtitle: string;
  status: Status;
  detail: string;
  detailTone?: string;
  meta: string;
  isTrigger?: boolean;
  isLast?: boolean;
  portTone?: string;
  still: boolean;
}) {
  const accent = nodeAccent(type);
  const state = status ? STATUS[status] : null;

  return (
    <div
      className="relative w-full max-w-[236px] overflow-hidden rounded-xl border shadow-[0_1px_2px_rgba(0,0,0,0.10),0_12px_24px_-16px_rgba(0,0,0,0.45)] transition-colors duration-300 md:w-[236px]"
      style={{
        // The identity colour runs faintly through the card, as on the canvas.
        background: `color-mix(in oklab, ${accent} 9%, var(--color-ink-850))`,
        borderColor:
          status === "running"
            ? "color-mix(in oklab, var(--series) 60%, transparent)"
            : "var(--edge-strong)",
      }}
    >
      <span
        className="absolute inset-y-0 left-0 w-[3px]"
        style={{ background: accent }}
        aria-hidden="true"
      />

      {/* Ports sit where the canvas puts them: on the sides when the flow runs
          left to right, top and bottom once it has stacked. */}
      {!isTrigger && (
        <span
          className="pointer-events-none absolute inset-x-0 top-0 flex justify-center md:inset-x-auto md:inset-y-0 md:left-0 md:items-center"
          aria-hidden="true"
        >
          <Port color="var(--color-ink-400)" />
        </span>
      )}
      {!isLast && (
        <span
          className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-center md:inset-x-auto md:inset-y-0 md:right-0 md:items-center"
          aria-hidden="true"
        >
          <Port color={portTone ?? "var(--color-brand-400)"} />
        </span>
      )}

      <div className="relative flex items-start gap-2.5 py-2.5 pr-3.5 pl-3">
        <NodeIconChip type={type} size={8} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[0.82rem] leading-tight font-medium text-ink-100">
            {name}
          </p>
          <p className="mt-1 truncate text-xs leading-tight text-ink-400">
            {subtitle}
          </p>
        </div>
      </div>

      <div className="relative border-t border-ink-700/60 px-3.5 py-2">
        <p
          className="flex h-4 items-center gap-1.5 text-xs"
          style={{ color: state?.color }}
        >
          {state && (
            <>
              {status === "running" ? (
                <motion.span
                  className="h-2 w-2 flex-none rounded-full"
                  style={{ background: state.color }}
                  animate={still ? undefined : { opacity: [1, 0.3, 1] }}
                  transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
                />
              ) : (
                <Check />
              )}
              <span className="font-medium">{state.label}</span>
              {detail && (
                <span
                  className="ml-auto truncate font-mono"
                  style={{ color: detailTone ?? "var(--color-ink-400)" }}
                >
                  {detail}
                </span>
              )}
            </>
          )}
        </p>
        {/* Reserved whether or not there is anything to say yet. */}
        <p className="mt-1 h-4 truncate font-mono text-[0.7rem] text-ink-400 tabular-nums">
          {meta}
        </p>
      </div>
    </div>
  );
}

function Port({ color }: { color: string }) {
  return (
    <span
      className="h-3.5 w-3.5 rounded-full border-2 border-[var(--color-ink-900)]"
      style={{ background: color }}
    />
  );
}

function Check() {
  return (
    <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none" aria-hidden="true">
      <path
        d="M2.5 6.4 4.8 8.7 9.5 3.9"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* --------------------------------------------------------------- edge --- */

const RUN = 72;
const DROP = 44;

/**
 * The line between two cards: the canvas dash, wiped in rather than stretched
 * (scaling a dash pattern smears it), with the moving dot the real edge has.
 * Both orientations are rendered and one is hidden, so the layout switches on
 * the same breakpoint as the cards with no resize listener in the middle.
 */
function Connector({ drawn, still }: { drawn: boolean; still: boolean }) {
  return (
    <>
      <Line drawn={drawn} still={still} vertical className="md:hidden" />
      <Line
        drawn={drawn}
        still={still}
        vertical={false}
        className="hidden md:flex"
      />
    </>
  );
}

function Line({
  drawn,
  still,
  vertical,
  className,
}: {
  drawn: boolean;
  still: boolean;
  vertical: boolean;
  className: string;
}) {
  const len = vertical ? DROP : RUN;
  const grown = drawn ? len : 0;
  const dash = `repeating-linear-gradient(${
    vertical ? "to bottom" : "to right"
  }, var(--series) 0 6px, transparent 6px 11px)`;

  return (
    <span
      className={`relative flex flex-none items-center justify-center ${
        vertical ? "h-11 w-6" : "h-6 w-[72px]"
      } ${className}`}
      aria-hidden="true"
    >
      <motion.span
        className="block overflow-hidden"
        initial={still ? false : vertical ? { height: 0 } : { width: 0 }}
        animate={vertical ? { height: grown } : { width: grown }}
        transition={{ duration: still ? 0 : 0.4, ease: "easeOut" }}
        style={vertical ? { width: 2.5 } : { height: 2.5 }}
      >
        <span
          className="block"
          style={{
            width: vertical ? 2.5 : len,
            height: vertical ? len : 2.5,
            backgroundImage: dash,
            opacity: 0.9,
          }}
        />
      </motion.span>

      {drawn && !still && (
        <motion.span
          className="absolute top-1/2 left-1/2 h-[7px] w-[7px] rounded-full"
          style={{
            marginTop: -3.5,
            marginLeft: -3.5,
            background: "var(--series)",
            boxShadow: "0 0 8px 1px color-mix(in oklab, var(--series) 55%, transparent)",
          }}
          initial={vertical ? { y: -len / 2 } : { x: -len / 2 }}
          animate={vertical ? { y: len / 2 } : { x: len / 2 }}
          transition={{
            duration: 1.1,
            repeat: Infinity,
            repeatDelay: 0.25,
            ease: "linear",
          }}
        />
      )}
    </span>
  );
}
