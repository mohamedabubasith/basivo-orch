/**
 * The run list — the log this product exists to give people.
 *
 * Failures are what anyone opens this page for, so the error is shown inline on
 * the row rather than behind a click. A list that makes you open six rows to
 * find out which one broke is a list that made you do its job.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Card, PageLoader } from "../../components/ui";
import { StatusPip } from "../../components/charts";
import { PageHeader, RelativeTime, duration } from "./bits";

type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

interface Run {
  id: string;
  flow_id: string;
  status: RunStatus;
  trigger: string;
  error: string | null;
  created_at: string;
  duration_ms: number | null;
}

const FILTERS = [
  { key: "all", label: "All" },
  { key: "failed", label: "Failed" },
  { key: "running", label: "Running" },
  { key: "succeeded", label: "Succeeded" },
] as const;

export function Runs() {
  const { orgId } = useWorkspace();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    // `run_status`, not `status`. FastAPI ignores unknown query params rather
    // than rejecting them, so the wrong name here is a filter that silently
    // returns everything and looks like it worked.
    const query = filter === "all" ? "" : `?run_status=${filter}`;
    try {
      setRuns(await api.get<Run[]>(`/api/v1/orgs/${orgId}/runs${query}`));
      setError(null);
    } catch {
      setError("Could not load runs.");
      setRuns([]);
    }
  }, [orgId, filter]);

  useEffect(() => {
    void load();
  }, [load]);

  // A run in flight changes state without the user doing anything, so the list
  // refreshes itself — but only while something is actually moving, rather
  // than polling an idle workspace forever.
  useEffect(() => {
    const inFlight = runs?.some((run) => run.status === "running" || run.status === "pending");
    if (!inFlight) return;
    const timer = setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [runs, load]);

  if (runs === null) return <PageLoader label="Loading runs" />;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Runs"
        subtitle="Every execution, with what it cost and what went wrong."
      />

      <div className="flex items-center gap-1 self-start rounded-xl border border-ink-700/60 bg-ink-900/50 p-1">
        {FILTERS.map((option) => (
          <button
            key={option.key}
            onClick={() => setFilter(option.key)}
            className={cx(
              "relative rounded-lg px-3 py-1.5 text-sm transition-colors",
              filter === option.key ? "text-ink-100" : "text-ink-400 hover:text-ink-200",
            )}
          >
            {filter === option.key && (
              <motion.span
                layoutId="run-filter-pill"
                className="absolute inset-0 rounded-lg bg-ink-800"
                transition={{ type: "spring", stiffness: 380, damping: 30 }}
              />
            )}
            <span className="relative">{option.label}</span>
          </button>
        ))}
      </div>

      {error && <Alert>{error}</Alert>}

      {runs.length === 0 ? (
        <Card className="p-10 text-center">
          <p className="text-ink-200">
            {filter === "all" ? "Nothing has run yet." : `No ${filter} runs.`}
          </p>
          {filter === "all" && (
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-500">
              Publish a flow and trigger it — from this app, an HTTP call or a
              schedule — and every execution shows up here with per-node timing.
            </p>
          )}
        </Card>
      ) : (
        <ul className="space-y-2">
          {runs.map((run, index) => (
            <motion.li
              key={run.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(index * 0.02, 0.2), duration: 0.22 }}
            >
              <div className="surface rounded-xl p-4">
                <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                  <StatusLabel status={run.status} />
                  <span className="font-mono text-xs text-ink-500">{run.id.slice(0, 8)}</span>
                  <span className="text-xs text-ink-500 capitalize">{run.trigger}</span>
                  <span className="ml-auto font-mono text-xs text-ink-300">
                    {duration(run.duration_ms)}
                  </span>
                  <span className="text-xs text-ink-500">
                    <RelativeTime value={run.created_at} />
                  </span>
                </div>
                {run.error && (
                  <p
                    className="mt-2.5 truncate border-t border-ink-700/50 pt-2.5 font-mono text-xs"
                    style={{ color: "var(--status-bad)" }}
                    title={run.error}
                  >
                    {run.error}
                  </p>
                )}
              </div>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StatusLabel({ status }: { status: RunStatus }) {
  if (status === "succeeded") return <StatusPip tone="good">Succeeded</StatusPip>;
  if (status === "failed") return <StatusPip tone="bad">Failed</StatusPip>;
  if (status === "cancelled") return <StatusPip tone="warn">Cancelled</StatusPip>;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-300">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-brand-400" />
      </span>
      {status === "running" ? "Running" : "Queued"}
    </span>
  );
}
