/**
 * The landing page's centrepiece: a pipeline run, streaming its own logs.
 *
 * This is a scripted simulation, not a live connection — it is marketing, and
 * it says so in the caption. It is built from the same shapes the real run
 * viewer will use (node status, levelled log lines, per-step duration) so the
 * screenshot in someone's head matches the product they sign up for.
 */

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useEffect, useRef, useState } from "react";

type NodeStatus = "pending" | "running" | "ok" | "failed" | "retrying";
type Level = "info" | "debug" | "warn" | "error" | "success";

interface PipelineNode {
  id: string;
  label: string;
  kind: string;
}

interface LogLine {
  key: number;
  at: string;
  level: Level;
  node: string;
  message: string;
  duration?: string;
}

const NODES: PipelineNode[] = [
  { id: "trigger", label: "Webhook", kind: "trigger" },
  { id: "enrich", label: "Enrich context", kind: "tool" },
  { id: "agent", label: "Triage agent", kind: "llm" },
  { id: "route", label: "Route", kind: "branch" },
  { id: "notify", label: "Notify team", kind: "action" },
];

/** One scripted run. `wait` is the delay *before* the step is applied. */
interface Step {
  wait: number;
  node?: string;
  status?: NodeStatus;
  log?: Omit<LogLine, "key" | "at">;
}

const SCRIPT: Step[] = [
  { wait: 300, node: "trigger", status: "running" },
  { wait: 260, log: { level: "info", node: "webhook", message: "run started · trace 8f21c4" } },
  { wait: 220, node: "trigger", status: "ok",
    log: { level: "success", node: "webhook", message: "payload accepted", duration: "12ms" } },

  { wait: 240, node: "enrich", status: "running" },
  { wait: 300, log: { level: "debug", node: "enrich", message: "GET /crm/customers/4471" } },
  { wait: 340, log: { level: "info", node: "enrich", message: "resolved plan=enterprise seats=240" } },
  { wait: 220, node: "enrich", status: "ok",
    log: { level: "success", node: "enrich", message: "context assembled", duration: "318ms" } },

  { wait: 240, node: "agent", status: "running" },
  { wait: 320, log: { level: "info", node: "agent", message: "claude-opus-5 · 1,284 prompt tokens" } },
  { wait: 420, log: { level: "warn", node: "agent", message: "rate limited upstream, backing off 400ms" } },
  { wait: 300, node: "agent", status: "retrying",
    log: { level: "info", node: "agent", message: "attempt 2 of 3" } },
  { wait: 460, log: { level: "info", node: "agent", message: "classified: billing · urgency high" } },
  { wait: 200, node: "agent", status: "ok",
    log: { level: "success", node: "agent", message: "completed", duration: "1.9s" } },

  { wait: 220, node: "route", status: "running" },
  { wait: 260, node: "route", status: "ok",
    log: { level: "info", node: "route", message: "branch → escalate", duration: "4ms" } },

  { wait: 240, node: "notify", status: "running" },
  { wait: 380, log: { level: "error", node: "notify", message: "slack 503 — service unavailable" } },
  { wait: 300, node: "notify", status: "retrying",
    log: { level: "info", node: "notify", message: "retry 1 · backoff 2s" } },
  { wait: 460, node: "notify", status: "ok",
    log: { level: "success", node: "notify", message: "posted to #support-escalations", duration: "740ms" } },

  { wait: 400, log: { level: "success", node: "run", message: "run finished · 5 steps · 3.1s" } },
  { wait: 2600 },
];

const LEVEL_STYLES: Record<Level, { dot: string; text: string; label: string }> = {
  info: { dot: "bg-brand-400", text: "text-ink-300", label: "INFO" },
  debug: { dot: "bg-ink-500", text: "text-ink-400", label: "DBUG" },
  warn: { dot: "bg-warn-500", text: "text-warn-500", label: "WARN" },
  error: { dot: "bg-err-500", text: "text-err-500", label: "ERR " },
  success: { dot: "bg-ok-500", text: "text-ok-500", label: "OK  " },
};

const STATUS_STYLES: Record<NodeStatus, string> = {
  pending: "border-ink-600/60 text-ink-500",
  running: "border-brand-400/70 text-brand-300 bg-brand-500/10",
  retrying: "border-warn-500/70 text-warn-500 bg-warn-500/10",
  ok: "border-ok-500/50 text-ok-500 bg-ok-500/[0.08]",
  failed: "border-err-500/60 text-err-500 bg-err-500/10",
};

const MAX_LINES = 9;

function clockAt(offsetMs: number): string {
  // Fixed base so the timestamps read like a real run without depending on
  // when the visitor happens to load the page.
  const base = 14 * 3600 + 32 * 60 + 8;
  const t = base + Math.floor(offsetMs / 1000);
  const hh = String(Math.floor(t / 3600) % 24).padStart(2, "0");
  const mm = String(Math.floor(t / 60) % 60).padStart(2, "0");
  const ss = String(t % 60).padStart(2, "0");
  const ms = String(offsetMs % 1000).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}

export function LogStream() {
  const reduceMotion = useReducedMotion();
  const [lines, setLines] = useState<LogLine[]>([]);
  const [statuses, setStatuses] = useState<Record<string, NodeStatus>>({});
  const [elapsed, setElapsed] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let index = 0;
    let clock = 0;
    let key = 0;

    const tick = () => {
      if (cancelled) return;
      const step = SCRIPT[index];

      timer = setTimeout(() => {
        if (cancelled) return;
        clock += step.wait;

        if (step.node && step.status) {
          const node = step.node;
          const status = step.status;
          setStatuses((prev) => ({ ...prev, [node]: status }));
        }
        if (step.log) {
          const entry: LogLine = { ...step.log, key: key++, at: clockAt(clock) };
          setLines((prev) => [...prev, entry].slice(-MAX_LINES));
        }
        setElapsed(clock);

        index += 1;
        if (index >= SCRIPT.length) {
          // Loop: clear and start over, so the panel is never static.
          index = 0;
          clock = 0;
          setLines([]);
          setStatuses({});
          setElapsed(0);
        }
        tick();
      }, step.wait);
    };

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const done = Object.values(statuses).filter((s) => s === "ok").length;
  const progress = (done / NODES.length) * 100;

  return (
    <div className="surface overflow-hidden rounded-2xl shadow-2xl shadow-black/50">
      {/* window chrome */}
      <div className="flex items-center gap-3 border-b border-ink-700/60 bg-ink-900/60 px-4 py-3">
        <div className="flex gap-1.5" aria-hidden="true">
          <span className="h-2.5 w-2.5 rounded-full bg-err-500/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-warn-500/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-ok-500/70" />
        </div>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="truncate font-mono text-xs text-ink-400">
            support-triage <span className="text-ink-600">/</span> run #4,218
          </span>
        </div>
        <span className="flex items-center gap-1.5 rounded-full bg-ok-500/10 px-2 py-0.5 font-mono text-[0.65rem] text-ok-500">
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ok-500 opacity-70" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-ok-500" />
          </span>
          live
        </span>
      </div>

      {/* node rail */}
      <div className="border-b border-ink-700/60 bg-ink-900/30 px-4 py-3">
        <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
          {NODES.map((node, i) => {
            const status = statuses[node.id] ?? "pending";
            return (
              <div key={node.id} className="flex flex-none items-center gap-1.5">
                <motion.div
                  animate={
                    status === "running" && !reduceMotion
                      ? { scale: [1, 1.03, 1] }
                      : { scale: 1 }
                  }
                  transition={{ duration: 1.1, repeat: status === "running" ? Infinity : 0 }}
                  className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 transition-colors duration-300 ${STATUS_STYLES[status]}`}
                >
                  <StatusGlyph status={status} />
                  <span className="text-[0.7rem] font-medium whitespace-nowrap">{node.label}</span>
                </motion.div>
                {i < NODES.length - 1 && (
                  <span
                    className={`h-px w-3 transition-colors duration-500 ${
                      statuses[node.id] === "ok" ? "bg-ok-500/50" : "bg-ink-600/60"
                    }`}
                    aria-hidden="true"
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* log body */}
      <div
        ref={scrollRef}
        className="h-[248px] overflow-hidden bg-ink-950/60 px-4 py-3 font-mono text-[0.72rem] leading-relaxed"
      >
        <AnimatePresence initial={false}>
          {lines.map((line) => {
            const style = LEVEL_STYLES[line.level];
            return (
              <motion.div
                key={line.key}
                initial={reduceMotion ? false : { opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.22, ease: "easeOut" }}
                className="flex items-baseline gap-2.5 py-[3px]"
              >
                <span className="flex-none text-ink-600">{line.at}</span>
                <span className={`flex-none font-semibold ${style.text}`}>{style.label}</span>
                <span className="flex-none text-ink-500">{line.node}</span>
                <span className={`min-w-0 flex-1 truncate ${style.text}`}>{line.message}</span>
                {line.duration && (
                  <span className="flex-none text-ink-600">{line.duration}</span>
                )}
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>

      {/* footer */}
      <div className="flex items-center gap-3 border-t border-ink-700/60 bg-ink-900/60 px-4 py-2.5">
        <div className="h-1 flex-1 overflow-hidden rounded-full bg-ink-700/70">
          <motion.div
            className="h-full rounded-full bg-gradient-to-r from-brand-500 to-accent-500"
            animate={{ width: `${progress}%` }}
            transition={{ duration: 0.5, ease: "easeOut" }}
          />
        </div>
        <span className="flex-none font-mono text-[0.65rem] text-ink-500">
          {done}/{NODES.length} steps · {(elapsed / 1000).toFixed(1)}s
        </span>
      </div>
    </div>
  );
}

function StatusGlyph({ status }: { status: NodeStatus }) {
  if (status === "ok") {
    return (
      <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none" aria-hidden="true">
        <path d="M2.5 6.4 4.8 8.7 9.5 3.9" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (status === "running" || status === "retrying") {
    return (
      <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none animate-spin" aria-hidden="true">
        <circle cx="6" cy="6" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeDasharray="16 10" strokeLinecap="round" />
      </svg>
    );
  }
  if (status === "failed") {
    return (
      <svg viewBox="0 0 12 12" className="h-3 w-3 flex-none" aria-hidden="true">
        <path d="M3.6 3.6l4.8 4.8M8.4 3.6l-4.8 4.8" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      </svg>
    );
  }
  return <span className="h-1.5 w-1.5 flex-none rounded-full bg-current opacity-60" />;
}
