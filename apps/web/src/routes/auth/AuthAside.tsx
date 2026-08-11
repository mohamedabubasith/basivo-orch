import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useState } from "react";

import { Logo } from "../../components/ui";

/**
 * The panel beside the sign-in form.
 *
 * It shows the product doing the one thing the product is for — a pipeline run
 * emitting its log — rather than a stock photograph or a wall of logos. Someone
 * arriving at a sign-in page for the first time has usually not seen the app,
 * and this is the cheapest honest look at it.
 *
 * Hidden below `lg`. On a phone it would push the form below the fold, and
 * nobody scrolls past decoration to reach a login box.
 */

const STEPS = [
  { node: "Webhook", detail: "run started", tone: "brand" },
  { node: "Enrich", detail: "context assembled · 318ms", tone: "ok" },
  { node: "Agent", detail: "classified: billing · urgency high", tone: "ok" },
  { node: "Notify", detail: "retry 1 · backoff 2s", tone: "warn" },
  { node: "Notify", detail: "posted to #support", tone: "ok" },
] as const;

const TONES = {
  brand: "text-brand-300 border-brand-400/40 bg-brand-500/10",
  ok: "text-ok-500 border-ok-500/35 bg-ok-500/[0.08]",
  warn: "text-warn-500 border-warn-500/40 bg-warn-500/10",
} as const;

export function AuthAside() {
  const reduceMotion = useReducedMotion();
  const [shown, setShown] = useState(reduceMotion ? STEPS.length : 0);

  useEffect(() => {
    if (reduceMotion) return;
    const timer = setInterval(() => {
      setShown((n) => (n >= STEPS.length ? 0 : n + 1));
    }, 1100);
    return () => clearInterval(timer);
  }, [reduceMotion]);

  return (
    <aside className="relative hidden overflow-hidden border-r border-ink-800/70 lg:block">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0">
        <div className="grid-bg absolute inset-0 opacity-[0.16] animate-grid [mask-image:radial-gradient(ellipse_at_30%_40%,black_20%,transparent_75%)]" />
        <div className="absolute -top-32 -left-24 h-[420px] w-[520px] rounded-full bg-brand-500/16 blur-[110px] animate-pulse-slow" />
        <div className="absolute right-[-10%] bottom-[-10%] h-[360px] w-[420px] rounded-full bg-accent-500/12 blur-[100px]" />
      </div>

      <div className="relative flex h-full flex-col justify-between p-10 xl:p-14">
        <Logo />

        <div className="max-w-md">
          <h2 className="text-[2rem] leading-[1.15] font-semibold tracking-tight text-balance text-ink-100">
            Agent pipelines you can
            <span className="text-gradient"> actually watch run</span>
          </h2>
          <p className="mt-4 text-[0.98rem] leading-relaxed text-pretty text-ink-400">
            Every step, every retry, every error — recorded and streamed, so a
            failure at 3am is something you read rather than guess at.
          </p>

          <div className="mt-8 space-y-2" aria-hidden="true">
            <AnimatePresence initial={false}>
              {STEPS.slice(0, shown).map((step, i) => (
                <motion.div
                  key={`${step.node}-${i}`}
                  initial={reduceMotion ? false : { opacity: 0, x: -12 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.28, ease: "easeOut" }}
                  className="flex items-center gap-3"
                >
                  <span
                    className={`flex-none rounded-lg border px-2 py-1 font-mono text-[0.68rem] ${TONES[step.tone]}`}
                  >
                    {step.node}
                  </span>
                  <span className="truncate font-mono text-[0.72rem] text-ink-500">
                    {step.detail}
                  </span>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        </div>

        <p className="text-xs text-ink-600">
          Beta — building in the open.
        </p>
      </div>
    </aside>
  );
}
