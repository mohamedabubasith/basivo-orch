import { motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";
import { BarList, Panel, StatTile, StatusPip, type BarDatum } from "../../components/charts";
import { formatMs, formatPercent } from "../../lib/viz";
import { Badge, Card, Spinner } from "../../components/ui";
import { RunsChart } from "./RunsChart";

interface NodeStat {
  node_id: string;
  node_type: string;
  node_name: string;
  executions: number;
  succeeded: number;
  failed: number;
  skipped: number;
  total_ms: number;
  share_of_runtime: number;
  p50_ms: number | null;
  p95_ms: number | null;
  failure_rate: number | null;
  retry_rescued: number;
}

interface Analytics {
  window_days: number;
  daily: { date: string; succeeded: number; failed: number; other: number }[];
  runs: {
    total: number;
    succeeded: number;
    failed: number;
    running: number;
    success_rate: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
  };
  retry_rescued_runs: number;
  nodes: NodeStat[];
  failure_clusters: {
    signature: string;
    example: string;
    node_type: string;
    node_name: string | null;
    count: number;
    last_seen: string | null;
  }[];
  dead_branches: { node_id: string; node_name: string; node_type: string; skipped: number }[];
}

const WINDOWS = [1, 7, 30] as const;

export function Dashboard() {
  const { user } = useAuth();
  // The shell guarantees a workspace before this renders, so there is no
  // "which org?" question left here and no branch for having none.
  const { orgId } = useWorkspace();
  const [data, setData] = useState<Analytics | null>(null);
  const [days, setDays] = useState<number>(7);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (organization: string, window: number) => {
      setLoading(true);
      try {
        setData(await api.get<Analytics>(`/api/v1/orgs/${organization}/analytics?days=${window}`));
        setError(null);
      } catch {
        setError("Could not load your analytics.");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (orgId) void load(orgId, days);
    // `days` deliberately excluded: the window switcher reloads directly, and
    // including it here would fetch twice on every change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId, load]);

  const runs = data?.runs;
  const hasRuns = (runs?.total ?? 0) > 0;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1.5 text-[0.68rem] font-medium tracking-[0.14em] text-brand-400 uppercase">
            Overview
          </p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-100">
            Welcome{user?.email ? `, ${user.email.split("@")[0]}` : ""}
          </h1>
          <p className="mt-1.5 text-ink-400">
            What your pipelines actually did in the last {data?.window_days ?? days} days.
          </p>
        </div>

        {/* Filters sit in one row above the charts. */}
        <div className="flex items-center gap-1 rounded-xl border border-ink-700/60 bg-ink-900/50 p-1">
          {WINDOWS.map((window) => (
            <button
              key={window}
              onClick={() => {
                setDays(window);
                if (orgId) void load(orgId, window);
              }}
              className={`relative rounded-lg px-3 py-1.5 text-sm transition-colors ${
                days === window ? "text-ink-100" : "text-ink-400 hover:text-ink-200"
              }`}
            >
              {days === window && (
                <motion.span
                  layoutId="window-pill"
                  className="absolute inset-0 rounded-lg bg-ink-800"
                  transition={{ type: "spring", stiffness: 380, damping: 30 }}
                />
              )}
              <span className="relative">{window}d</span>
            </button>
          ))}
        </div>
      </header>

      {loading && !data && (
        <div className="flex items-center gap-3 py-16 text-ink-400">
          <Spinner className="h-5 w-5" />
          <span className="text-sm">Loading your analytics…</span>
        </div>
      )}

      {error && !loading && (
        <Card className="p-8 text-center">
          <p className="text-ink-300">{error}</p>
        </Card>
      )}

      {data && !hasRuns && <EmptyState />}

      {data && hasRuns && runs && (
        <>
          {/* KPI row. Four headline numbers are stat tiles, not a grouped bar
              chart — the reader wants each value, not a comparison between
              them. One segmented surface rather than four floating cards: an
              instrument strip reads as one panel, and it is the composition
              every modern dashboard uses for its headline row. */}
          <div className="surface grid overflow-hidden rounded-xl sm:grid-cols-2 lg:grid-cols-4 [&>*+*]:border-t [&>*+*]:border-[var(--edge)] sm:[&>*]:border-t-0 sm:[&>*:nth-child(even)]:border-l sm:[&>*:nth-child(n+3)]:border-t lg:[&>*]:!border-t-0 lg:[&>*+*]:!border-l lg:[&>*+*]:border-[var(--edge)]">
            <StatTile flat label="Runs" value={runs.total.toLocaleString()} hint={`${runs.running} in flight`} />
            <StatTile
              flat
              label="Success rate"
              value={formatPercent(runs.success_rate, 1)}
              hint={`${runs.failed} failed`}
              tone={
                runs.success_rate === null
                  ? undefined
                  : runs.success_rate >= 0.99
                    ? "good"
                    : runs.success_rate >= 0.9
                      ? "warn"
                      : "bad"
              }
            />
            <StatTile
              flat
              label="Typical duration"
              value={formatMs(runs.p50_ms)}
              hint={`p95 ${formatMs(runs.p95_ms)} — the slow tail your users feel`}
            />
            {/* The differentiator. Every dashboard that counts final states
                reports these runs as clean successes. */}
            <StatTile
              flat
              label="Saved by a retry"
              value={data.retry_rescued_runs.toLocaleString()}
              tone={data.retry_rescued_runs > 0 ? "warn" : "good"}
              hint={
                data.retry_rescued_runs > 0
                  ? "Runs that only succeeded on a second attempt. They count as successes everywhere else."
                  : "No run needed a retry to succeed."
              }
            />
          </div>

          <div className="surface rounded-2xl p-5">
            <RunsChart daily={data.daily ?? []} />
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel
              title="Where the time goes"
              description="Share of total run time by node. “The flow is slow” is not actionable; one node at 78% is."
            >
              <BarList
                data={data.nodes.slice(0, 5).map<BarDatum>((node, i) => ({
                  key: node.node_id,
                  label: node.node_name,
                  value: node.share_of_runtime,
                  display: formatPercent(node.share_of_runtime, 1),
                  meta: `p50 ${formatMs(node.p50_ms)} · p95 ${formatMs(node.p95_ms)} · ${node.node_type}`,
                  emphasis: i === 0 && node.share_of_runtime > 0.4,
                }))}
                caption="Ordered by total time. Only the top node is highlighted, and only when it dominates."
              />
            </Panel>

            <Panel
              title="Reliability by node"
              description="Failure rate per node, and how often a retry rescued it."
            >
              <BarList
                emptyLabel="Not enough executions to rate reliability yet"
                data={data.nodes
                  .filter((n) => n.failure_rate !== null)
                  // Sorted by failure rate, not by runtime. This panel's job is
                  // "what is broken"; leading with a healthy node because it
                  // happens to be slow buries the answer.
                  .sort((a, b) => (b.failure_rate ?? 0) - (a.failure_rate ?? 0))
                  .slice(0, 5)
                  .map<BarDatum>((node) => ({
                    key: node.node_id,
                    label: node.node_name,
                    value: node.failure_rate ?? 0,
                    display: formatPercent(node.failure_rate, 1),
                    meta:
                      node.retry_rescued > 0
                        ? `${node.retry_rescued} rescued by retry · ${node.executions} executions`
                        : `${node.executions} executions`,
                    tone:
                      (node.failure_rate ?? 0) > 0.2
                        ? "bad"
                        : (node.failure_rate ?? 0) > 0.02
                          ? "warn"
                          : "good",
                  }))}
                caption="A rate is withheld below five executions — three failures out of four is not a 75% failure rate worth chasing."
              />
            </Panel>
          </div>

          <Panel
            title="Distinct failures"
            description="Grouped by error shape, not by run. Forty failed runs are usually two causes."
          >
            {data.failure_clusters.length === 0 ? (
              <p className="py-8 text-center text-sm text-ink-500">
                Nothing failed in this window.
              </p>
            ) : (
              /* Past a handful of classes a table beats more colours. */
              <div className="-mx-2 overflow-x-auto">
                <table className="w-full min-w-[34rem] text-left text-sm">
                  <thead>
                    <tr className="border-b border-ink-700/60 text-xs text-ink-500">
                      <th className="px-2 pb-2 font-medium">Error</th>
                      <th className="px-2 pb-2 font-medium">Node</th>
                      <th className="px-2 pb-2 text-right font-medium">Occurrences</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.failure_clusters.map((cluster) => (
                      <tr
                        key={cluster.signature}
                        className="border-b border-ink-800/60 last:border-0"
                      >
                        <td className="max-w-[26rem] px-2 py-3">
                          <p className="truncate font-mono text-xs text-ink-200">
                            {cluster.example}
                          </p>
                        </td>
                        <td className="px-2 py-3 text-ink-400">{cluster.node_name ?? "—"}</td>
                        <td className="px-2 py-3 text-right">
                          <StatusPip tone={cluster.count > 5 ? "bad" : "warn"}>
                            {cluster.count}
                          </StatusPip>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          {data.dead_branches.length > 0 && (
            <Panel
              title="Branches that never fire"
              description="Skipped on every run in this window — dead logic, or a path nothing has exercised. Neither shows up in a success rate."
            >
              <ul className="flex flex-wrap gap-2">
                {data.dead_branches.map((branch) => (
                  <li key={branch.node_id}>
                    <Badge>
                      {branch.node_name}
                      <span className="text-ink-500">· skipped {branch.skipped}×</span>
                    </Badge>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="space-y-6">
      <Card className="p-10 text-center">
        <div className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-2xl border border-ink-700/70 bg-ink-850">
          <svg viewBox="0 0 24 24" className="h-5 w-5 text-brand-300" fill="none" aria-hidden="true">
            <path
              d="M4 7h6M14 7h6M4 17h6M14 17h6M10 7a2 2 0 002 2h0a2 2 0 012 2v2a2 2 0 002 2"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        </div>
        <h2 className="text-lg font-semibold text-ink-100">No runs yet</h2>
        <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-400">
          Analytics appear as soon as a flow runs. Every node execution is
          recorded — timing, retries and errors — so this page has something to
          say from the very first run.
        </p>
        <p className="mx-auto mt-4 max-w-md text-xs leading-relaxed text-ink-600">
          Flows are created through the API in this beta; the visual canvas is
          not built yet.
        </p>
      </Card>

    </div>
  );
}
