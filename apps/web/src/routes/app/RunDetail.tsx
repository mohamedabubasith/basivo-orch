/**
 * One run, in full: every node execution, and every step inside it.
 *
 * Two different questions live on this page. `nodes` (from `RunDetail`)
 * answers "what ran, in what order, with what outcome" — the shape the SOW's
 * per-node log always had. `events` (from the new `/runs/{id}/events`
 * endpoint) answers the finer question this product exists to answer for an
 * agent step: which model call happened, which tool it invoked, what that
 * tool returned, how many tokens each turn cost. A node execution is one row;
 * an agent's events inside it are a sequence, and flattening that sequence
 * into the row would lose the order and the per-turn cost.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Card, PageLoader } from "../../components/ui";
import { StatusPip } from "../../components/charts";
import { PageHeader, RelativeTime, duration } from "./bits";

type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";
type NodeStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

interface NodeExecution {
  node_id: string;
  node_type: string;
  node_name: string | null;
  status: NodeStatus;
  attempt: number;
  error: string | null;
  duration_ms: number | null;
  cost_usd: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  started_at: string;
  finished_at: string | null;
}

interface RunDetailResponse {
  id: string;
  flow_id: string;
  status: RunStatus;
  trigger: string;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  nodes: NodeExecution[];
}

interface RunEvent {
  seq: number;
  type: string;
  data: Record<string, unknown>;
  at: string;
}

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const { orgId } = useWorkspace();
  const [run, setRun] = useState<RunDetailResponse | null>(null);
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!orgId || !runId) return;
    try {
      const [detail, eventLog] = await Promise.all([
        api.get<RunDetailResponse>(`/api/v1/orgs/${orgId}/runs/${runId}`),
        api.get<{ events: RunEvent[] }>(`/api/v1/orgs/${orgId}/runs/${runId}/events`),
      ]);
      setRun(detail);
      setEvents(eventLog.events);
      setError(null);
    } catch {
      setError("Could not load this run.");
    }
  }, [orgId, runId]);

  useEffect(() => {
    void load();
  }, [load]);

  // A run still in flight is still writing events; poll until it settles
  // rather than leaving the page looking finished while work continues.
  useEffect(() => {
    if (!run || run.status === "succeeded" || run.status === "failed" || run.status === "cancelled") {
      return;
    }
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [run, load]);

  if (error) return <Alert>{error}</Alert>;
  if (!run || events === null) return <PageLoader label="Loading run" />;


  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2.5">
        <Link to="/app/runs" className="text-ink-500 transition-colors hover:text-ink-200">
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
            <path
              d="M14 7l-5 5 5 5"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </Link>
        <PageHeader title="Run detail" />
      </div>

      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <RunStatusLabel status={run.status} />
          <Field label="Run" value={run.id.slice(0, 8)} mono />
          <Field label="Trigger" value={run.trigger} capitalize />
          <Field label="Duration" value={duration(run.duration_ms)} mono />
          <Field label="Started" value={<RelativeTime value={run.started_at} />} />
        </div>
        {run.error && (
          <p
            className="mt-4 rounded-lg border p-3 font-mono text-xs leading-relaxed"
            style={{
              borderColor: "color-mix(in oklab, var(--status-bad) 40%, transparent)",
              color: "var(--status-bad)",
            }}
          >
            {run.error}
          </p>
        )}
      </Card>

      <div>
        <h2 className="mb-3 text-sm font-medium text-ink-300">Nodes</h2>
        <ul className="space-y-2">
          {run.nodes.map((node) => (
            <li key={`${node.node_id}-${node.attempt}`} className="surface rounded-xl p-4">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <NodeStatusLabel status={node.status} />
                <span className="font-medium text-ink-100">{node.node_name ?? node.node_id}</span>
                <span className="font-mono text-xs text-ink-500">{node.node_type}</span>
                {node.attempt > 1 && (
                  <span className="text-xs" style={{ color: "var(--status-warn)" }}>
                    attempt {node.attempt}
                  </span>
                )}
                <span className="ml-auto font-mono text-xs text-ink-300">
                  {duration(node.duration_ms)}
                </span>
              </div>

              {(node.tokens_in !== null || node.cost_usd !== null) && (
                <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1 border-t border-ink-700/50 pt-2.5 text-xs text-ink-400">
                  {node.tokens_in !== null && (
                    <span>
                      <span className="text-ink-500">tokens in</span>{" "}
                      <span className="font-mono text-ink-200">{node.tokens_in.toLocaleString()}</span>
                    </span>
                  )}
                  {node.tokens_out !== null && (
                    <span>
                      <span className="text-ink-500">tokens out</span>{" "}
                      <span className="font-mono text-ink-200">{node.tokens_out.toLocaleString()}</span>
                    </span>
                  )}
                  {node.cost_usd !== null && (
                    <span>
                      <span className="text-ink-500">cost</span>{" "}
                      <span className="font-mono text-ink-200">${node.cost_usd.toFixed(4)}</span>
                    </span>
                  )}
                </div>
              )}

              {node.error && (
                <p
                  className="mt-2.5 border-t border-ink-700/50 pt-2.5 font-mono text-xs leading-relaxed"
                  style={{ color: "var(--status-bad)" }}
                >
                  {node.error}
                </p>
              )}

              <NodeSteps nodeId={node.node_id} events={events} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/**
 * The steps that happened inside one node — an agent's model calls and tool
 * calls, in order. Not shown for nodes with no steps: an HTTP node or a
 * condition has nothing here, and an empty "Steps" section under every row
 * would be noise repeated for every node in every run.
 */
function NodeSteps({ nodeId, events }: { nodeId: string; events: RunEvent[] }) {
  const steps = events.filter(
    (event) => event.data.node_id === nodeId && event.type.startsWith("node.step"),
  );
  if (steps.length === 0) return null;

  return (
    <div className="mt-3 space-y-1.5 border-t border-ink-700/50 pt-3">
      {steps.map((event) => (
        <StepRow key={event.seq} event={event} />
      ))}
    </div>
  );
}

const STEP_LABEL: Record<string, { label: string; tone: "good" | "warn" | "bad" | "neutral" }> = {
  "agent.started": { label: "Agent started", tone: "neutral" },
  "llm.response": { label: "Model call", tone: "neutral" },
  "tool.called": { label: "Tool called", tone: "neutral" },
  "tool.result": { label: "Tool result", tone: "good" },
  "agent.finished": { label: "Agent finished", tone: "good" },
  "agent.truncated": { label: "Agent stopped early", tone: "warn" },
};

function StepRow({ event }: { event: RunEvent }) {
  const step = String(event.data.step ?? "");
  const meta = STEP_LABEL[step] ?? { label: step || event.type, tone: "neutral" as const };
  const data = event.data;

  return (
    <div className="rounded-lg bg-ink-950/40 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {meta.tone === "neutral" ? (
          <span className="font-medium text-ink-300">{meta.label}</span>
        ) : (
          <StatusPip tone={meta.tone === "bad" ? "bad" : meta.tone}>{meta.label}</StatusPip>
        )}

        {step === "tool.called" && (
          <>
            <span className="font-mono text-ink-200">{String(data.tool)}</span>
            <span className="truncate text-ink-500">
              {JSON.stringify(data.arguments).slice(0, 80)}
            </span>
          </>
        )}
        {step === "tool.result" && (
          <>
            <span className="font-mono text-ink-200">{String(data.tool)}</span>
            <span className={data.ok ? "text-ink-500" : ""} style={data.ok ? undefined : { color: "var(--status-bad)" }}>
              {data.ok ? "ok" : "failed"}
            </span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-500">{duration(data.duration_ms)}</span>
            )}
          </>
        )}
        {step === "llm.response" && (
          <>
            <span className="font-mono text-ink-400">{String(data.model ?? "")}</span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-500">{duration(data.duration_ms)}</span>
            )}
            {typeof data.input_tokens === "number" && (
              <span className="font-mono text-ink-500">
                {data.input_tokens}→{Number(data.output_tokens ?? 0)} tok
              </span>
            )}
          </>
        )}
        {step === "agent.finished" && (
          <>
            {typeof data.tool_calls === "number" && (
              <span className="text-ink-500">{data.tool_calls} tool call(s)</span>
            )}
            {typeof data.cost_usd === "number" && (
              <span className="font-mono text-ink-200">${Number(data.cost_usd).toFixed(4)}</span>
            )}
          </>
        )}

        <span className="ml-auto shrink-0 text-ink-600">
          <RelativeTime value={event.at} />
        </span>
      </div>

      {step === "tool.result" && typeof data.result_preview === "string" && (
        <p className="mt-1 truncate font-mono text-ink-500">{data.result_preview}</p>
      )}
      {step === "llm.response" && typeof data.text_preview === "string" && data.text_preview && (
        <p className="mt-1 truncate text-ink-500">{data.text_preview}</p>
      )}
      {step === "agent.truncated" && typeof data.reason === "string" && (
        <p className="mt-1" style={{ color: "var(--status-warn)" }}>
          {data.reason}
        </p>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  mono,
  capitalize,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  capitalize?: boolean;
}) {
  return (
    <span className="text-sm">
      <span className="text-ink-500">{label}</span>{" "}
      <span className={`text-ink-200 ${mono ? "font-mono" : ""} ${capitalize ? "capitalize" : ""}`}>
        {value}
      </span>
    </span>
  );
}

function RunStatusLabel({ status }: { status: RunStatus }) {
  if (status === "succeeded") return <StatusPip tone="good">Succeeded</StatusPip>;
  if (status === "failed") return <StatusPip tone="bad">Failed</StatusPip>;
  if (status === "cancelled") return <StatusPip tone="warn">Cancelled</StatusPip>;
  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-ink-300">
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-brand-400" />
      </span>
      {status === "running" ? "Running" : "Queued"}
    </span>
  );
}

function NodeStatusLabel({ status }: { status: NodeStatus }) {
  if (status === "succeeded") return <StatusPip tone="good">Succeeded</StatusPip>;
  if (status === "failed") return <StatusPip tone="bad">Failed</StatusPip>;
  if (status === "skipped") return <StatusPip tone="warn">Skipped</StatusPip>;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-300">
      <span className="h-2 w-2 rounded-full bg-brand-400" />
      {status === "running" ? "Running" : "Pending"}
    </span>
  );
}
