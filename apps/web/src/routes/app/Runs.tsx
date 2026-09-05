/**
 * The run list: the log this product exists to give people.
 *
 * Failures are what anyone opens this page for, so a failed run's error sits
 * on its row rather than behind a click. A list that makes you open six rows
 * to find out which one broke is a list that made you do its job.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { NodeIcon } from "../../builder/nodeIcons";
import {
  Alert,
  Card,
  EmptyState,
  IconChip,
  PageLoader,
  Pill,
  type Tone,
} from "../../components/ui";
import { api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { PageHeader, RelativeTime, duration } from "./bits";

interface Run {
  id: string;
  flow_id: string;
  status: string;
  trigger: string;
  error: string | null;
  created_at: string;
  started_at: string | null;
  duration_ms: number | null;
}

interface FlowOption {
  id: string;
  name: string;
}

const FILTERS = [
  { key: "all", label: "All" },
  { key: "failed", label: "Failed" },
  { key: "running", label: "Running" },
  { key: "succeeded", label: "Succeeded" },
] as const;
type Filter = (typeof FILTERS)[number]["key"];

/** Status as a word and a tone. The engine says `queued`; older rows may say `pending`. */
const STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "warn" },
  pending: { label: "Queued", tone: "warn" },
  running: { label: "Running", tone: "warn" },
  succeeded: { label: "Succeeded", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

/**
 * What started the run: the word and the hue, matching the Flows list so the
 * two pages read as siblings. Manual, webhook and schedule borrow the canvas
 * node's glyph; API and Telegram have no canvas node and carry their own.
 */
const TRIGGERS: Record<string, { label: string; hue: string; glyph?: ReactNode }> =
  {
    manual: { label: "Manual", hue: "var(--color-brand-400)" },
    webhook: { label: "Webhook", hue: "var(--color-accent-500)" },
    schedule: { label: "Scheduled", hue: "var(--status-warn)" },
    api: {
      label: "API",
      hue: "var(--series)",
      glyph: (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M8.5 8 5 12l3.5 4M15.5 8 19 12l-3.5 4M13.2 6.5l-2.4 11" />
        </svg>
      ),
    },
    telegram: {
      label: "Telegram",
      hue: "#0ea5e9",
      glyph: (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 3 3 10.5l7.5 3 3 7.5L21 3Z" />
          <path d="M10.5 13.5 21 3" />
        </svg>
      ),
    },
  };

function triggerLabel(trigger: string): string {
  if (!trigger) return "Unknown";
  return (
    TRIGGERS[trigger]?.label ??
    trigger.charAt(0).toUpperCase() + trigger.slice(1)
  );
}

const RUNS_GLYPH = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="8" />
    <path d="M10 8.6v6.8l5.5-3.4L10 8.6Z" />
  </svg>
);

export function Runs() {
  const { orgId } = useWorkspace();
  const navigate = useNavigate();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [flows, setFlows] = useState<FlowOption[]>([]);
  const [flowId, setFlowId] = useState<string>("all");
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!orgId) return;
    void api
      .get<FlowOption[]>(`/api/v1/orgs/${orgId}/flows`)
      .then(setFlows)
      .catch(() => setFlows([]));
  }, [orgId]);

  const load = useCallback(async () => {
    if (!orgId) return;
    // `run_status`, not `status`. FastAPI ignores unknown query params rather
    // than rejecting them, so the wrong name here is a filter that silently
    // returns everything and looks like it worked.
    const params = new URLSearchParams();
    if (filter !== "all") params.set("run_status", filter);
    if (flowId !== "all") params.set("flow_id", flowId);
    const query = params.toString() ? `?${params.toString()}` : "";
    try {
      setRuns(await api.get<Run[]>(`/api/v1/orgs/${orgId}/runs${query}`));
      setError(null);
    } catch {
      setError("Could not load runs.");
      setRuns([]);
    }
  }, [orgId, filter, flowId]);

  useEffect(() => {
    void load();
  }, [load]);

  // A run in flight changes state without the user doing anything, so the list
  // refreshes itself, but only while something is actually moving, rather
  // than polling an idle workspace forever.
  useEffect(() => {
    const inFlight = runs?.some(
      (run) =>
        run.status === "running" ||
        run.status === "queued" ||
        run.status === "pending",
    );
    if (!inFlight) return;
    const timer = setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [runs, load]);

  if (runs === null) return <PageLoader label="Loading runs" />;

  const flowName = new Map(flows.map((flow) => [flow.id, flow.name]));
  const filtered = filter !== "all" || flowId !== "all";

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Observe"
        title="Runs"
        subtitle="Every execution: what started it, how long it took and what went wrong."
      />

      {error && <Alert>{error}</Alert>}

      {runs.length === 0 && !filtered ? (
        <EmptyState icon={RUNS_GLYPH} title="No runs yet">
          Press Test in the builder, or fire a published trigger, and the run
          appears here with every node's timing.
        </EmptyState>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div
              role="radiogroup"
              aria-label="Filter by status"
              className="flex items-center gap-0.5 rounded-xl border border-ink-700/60 bg-ink-900/50 p-0.5"
            >
              {FILTERS.map((segment) => {
                const active = filter === segment.key;
                return (
                  <button
                    key={segment.key}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => setFilter(segment.key)}
                    className={cx(
                      "relative rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
                      active
                        ? "text-ink-100"
                        : "text-ink-300 hover:text-ink-100",
                    )}
                  >
                    {active && (
                      <motion.span
                        layoutId="runs-filter-pill"
                        className="absolute inset-0 rounded-lg bg-ink-800"
                        transition={{
                          type: "spring",
                          stiffness: 400,
                          damping: 32,
                        }}
                      />
                    )}
                    <span className="relative">{segment.label}</span>
                  </button>
                );
              })}
            </div>

            <label className="block w-full sm:w-64">
              <span className="sr-only">Filter by flow</span>
              <select
                value={flowId}
                onChange={(event) => setFlowId(event.target.value)}
                className={cx(
                  "w-full rounded-xl border border-ink-600/70 bg-ink-900/70 px-3 py-2 text-sm text-ink-100",
                  "transition-all duration-150 hover:border-ink-500",
                  "focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none",
                )}
              >
                <option value="all">All flows</option>
                {flows.map((flow) => (
                  <option key={flow.id} value={flow.id}>
                    {flow.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Scroll rather than clip: a table cannot shrink below its content. */}
          <Card className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--edge)] bg-ink-900/40 text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
                  <th scope="col" className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
                    Run
                  </th>
                  <th scope="col" className="px-3 py-3 md:px-4">
                    Status
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Trigger
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Started
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Duration
                  </th>
                  <th scope="col" className="hidden py-3 pr-5 pl-1 md:table-cell">
                    <span className="sr-only">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run, index) => {
                  const status = STATUS[run.status] ?? {
                    label: run.status,
                    tone: "neutral" as Tone,
                  };
                  const trigger = TRIGGERS[run.trigger];
                  const name =
                    flowName.get(run.flow_id) ??
                    `Flow ${run.flow_id.slice(0, 8)}`;
                  return (
                    <motion.tr
                      key={run.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        delay: Math.min(index * 0.02, 0.2),
                        duration: 0.2,
                      }}
                      // The whole row opens the run; the name link is the
                      // keyboard route, so a click on it is left to the link.
                      onClick={(event) => {
                        if ((event.target as HTMLElement).closest("a")) return;
                        navigate(`/app/runs/${run.id}`);
                      }}
                      className="group cursor-pointer border-b border-[var(--edge)] transition-colors last:border-b-0 hover:bg-ink-800/40"
                    >
                      <td className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
                        <div className="flex min-w-0 items-center gap-3.5">
                          <IconChip
                            hue={trigger?.hue ?? "var(--series)"}
                            className="max-sm:hidden"
                          >
                            {trigger?.glyph ?? (
                              <NodeIcon type={`trigger.${run.trigger}`} />
                            )}
                          </IconChip>
                          <div className="min-w-0">
                            <Link
                              to={`/app/runs/${run.id}`}
                              className="font-medium text-ink-100 transition-colors hover:text-brand-300"
                            >
                              {name}
                            </Link>
                            <p className="mt-0.5 text-xs text-ink-400">
                              <span className="font-mono">
                                {run.id.slice(0, 8)}
                              </span>
                              {/* The columns hidden on small screens, folded
                                  into the one that stays. */}
                              <span className="md:hidden">
                                {" · "}
                                {triggerLabel(run.trigger)}
                                {" · "}
                                {duration(run.duration_ms)}
                                {" · "}
                                <RelativeTime
                                  value={run.started_at ?? run.created_at}
                                />
                              </span>
                            </p>
                            {/* One line with an ellipsis, but wrapping rather
                                than nowrap: nowrap text sets the column's
                                minimum width, and on a phone that pushed the
                                status column out of view. */}
                            {run.error && (
                              <p
                                className="mt-1 line-clamp-1 font-mono text-xs wrap-anywhere md:max-w-md"
                                style={{ color: "var(--status-bad)" }}
                                title={run.error}
                              >
                                {run.error}
                              </p>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-3 md:px-4">
                        <Pill
                          tone={status.tone}
                          dot
                          className={
                            run.status === "running"
                              ? "[&>span]:animate-pulse"
                              : undefined
                          }
                        >
                          {status.label}
                        </Pill>
                      </td>
                      <td className="hidden px-4 py-3 text-ink-300 md:table-cell">
                        {triggerLabel(run.trigger)}
                      </td>
                      <td className="hidden px-4 py-3 whitespace-nowrap text-ink-300 md:table-cell">
                        <RelativeTime value={run.started_at ?? run.created_at} />
                      </td>
                      <td className="hidden px-4 py-3 font-mono text-ink-300 tabular-nums md:table-cell">
                        {duration(run.duration_ms)}
                      </td>
                      <td className="hidden py-3 pr-5 pl-1 md:table-cell">
                        <svg
                          viewBox="0 0 24 24"
                          className="ml-auto h-4 w-4 text-ink-400 transition-colors group-hover:text-ink-100"
                          fill="none"
                          aria-hidden="true"
                        >
                          <path
                            d="M10 7l5 5-5 5"
                            stroke="currentColor"
                            strokeWidth="1.8"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          />
                        </svg>
                      </td>
                    </motion.tr>
                  );
                })}
              </tbody>
            </table>

            {runs.length === 0 && (
              <p className="px-5 py-10 text-center text-sm text-ink-400">
                No runs match this filter.
                <button
                  type="button"
                  onClick={() => {
                    setFilter("all");
                    setFlowId("all");
                  }}
                  className="ml-1.5 font-medium text-brand-300 hover:underline"
                >
                  Clear
                </button>
              </p>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
