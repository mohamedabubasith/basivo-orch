/**
 * One run, in full: every node execution, and every step inside it.
 *
 * This page is the product's claim made visible. Three layers, from coarse to
 * fine: the run (its input and final output, verbatim), each node (status,
 * attempt, duration, tokens, cost — and its recorded input and output, which
 * is the part every other tool hides), and inside an agent node, every model
 * turn and tool call as ordered steps.
 *
 * Node input/output arrive as `summarise()` envelopes — `{kind, preview,
 * keys|length}` — because payloads can be megabytes and the log table must
 * not become the biggest thing in the database. The UI unwraps the envelope:
 * the preview is shown as data, the envelope becomes a badge ("object · 14
 * keys"), and truncation is said out loud rather than passed off as the whole
 * value.
 *
 * Failed nodes arrive pre-expanded: the person opening this page at 3am came
 * to read exactly that one.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Card, PageLoader } from "../../components/ui";
import { StatusPip } from "../../components/charts";
import { NodeIconChip } from "../../builder/nodeIcons";
import { PageHeader, RelativeTime, duration } from "./bits";

type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";
type NodeStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

interface Summary {
  kind: string;
  preview?: unknown;
  value?: unknown;
  keys?: number;
  length?: number;
}

interface NodeExecution {
  node_id: string;
  node_type: string;
  node_name: string | null;
  status: NodeStatus;
  attempt: number;
  input_summary: Summary | null;
  output_summary: Summary | null;
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
        api.get<{ events: RunEvent[] }>(
          `/api/v1/orgs/${orgId}/runs/${runId}/events`,
        ),
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
    if (!run || ["succeeded", "failed", "cancelled"].includes(run.status))
      return;
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [run, load]);

  if (error) return <Alert>{error}</Alert>;
  if (!run || events === null) return <PageLoader label="Loading run" />;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2.5">
        <Link
          to="/app/runs"
          className="text-ink-500 transition-colors hover:text-ink-200"
        >
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
          <Field
            label="Started"
            value={<RelativeTime value={run.started_at} />}
          />
        </div>
        {run.error && (
          <p
            className="mt-4 rounded-lg border p-3 font-mono text-xs leading-relaxed"
            style={{
              borderColor:
                "color-mix(in oklab, var(--status-bad) 40%, transparent)",
              color: "var(--status-bad)",
            }}
          >
            {run.error}
          </p>
        )}

        {/* The run's own boundary values, verbatim — not summaries. */}
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          <JsonBlock title="Run input" value={run.input} />
          <JsonBlock
            title="Final output"
            value={run.output}
            empty="No output. The run did not finish."
          />
        </div>
      </Card>

      <div>
        <h2 className="mb-3 text-sm font-medium text-ink-300">Nodes</h2>
        <ul className="space-y-2">
          {run.nodes.map((node) => (
            <NodeRow
              key={`${node.node_id}-${node.attempt}`}
              node={node}
              events={events}
              // The failed node is the reason this page is open.
              defaultOpen={node.status === "failed"}
              orgId={orgId}
            />
          ))}
        </ul>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- node row --- */

function NodeRow({
  node,
  events,
  defaultOpen,
  orgId,
}: {
  node: NodeExecution;
  events: RunEvent[];
  defaultOpen: boolean;
  /** Needed to build artifact URLs — files are tenant-scoped. */
  orgId: string | null;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const steps = events.filter(
    (event) =>
      event.data.node_id === node.node_id && event.type === "node.step",
  );

  return (
    <li className="surface overflow-hidden rounded-xl">
      <button
        onClick={() => setOpen((value) => !value)}
        className="flex w-full flex-wrap items-center gap-x-4 gap-y-2 p-4 text-left transition-colors hover:bg-ink-850/40"
      >
        <NodeIconChip type={node.node_type} size={8} />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-ink-100">
            {node.node_name ?? node.node_id}
          </p>
          <p className="truncate font-mono text-[0.65rem] text-ink-500">
            {node.node_type}
          </p>
        </div>
        <NodeStatusLabel status={node.status} />
        {node.attempt > 1 && (
          <span className="text-xs" style={{ color: "var(--status-warn)" }}>
            attempt {node.attempt}
          </span>
        )}
        {node.tokens_in !== null && (
          <span className="font-mono text-xs text-ink-400">
            {node.tokens_in.toLocaleString()}→
            {(node.tokens_out ?? 0).toLocaleString()} tok
          </span>
        )}
        {node.cost_usd !== null && (
          <span className="font-mono text-xs text-ink-300">
            ${node.cost_usd.toFixed(4)}
          </span>
        )}
        <span className="font-mono text-xs text-ink-300">
          {duration(node.duration_ms)}
        </span>
        <svg
          viewBox="0 0 24 24"
          className={cx(
            "h-4 w-4 flex-none text-ink-500 transition-transform",
            open && "rotate-180",
          )}
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M6 9l6 6 6-6"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
          />
        </svg>
      </button>

      {open && (
        <div className="space-y-3 border-t border-ink-700/60 p-4">
          {node.error && (
            <p
              className="rounded-lg border p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap"
              style={{
                borderColor:
                  "color-mix(in oklab, var(--status-bad) 40%, transparent)",
                color: "var(--status-bad)",
              }}
            >
              {node.error}
            </p>
          )}

          <div className="grid gap-3 lg:grid-cols-2">
            <SummaryBlock title="Input" summary={node.input_summary} />
            <SummaryBlock title="Output" summary={node.output_summary} />
          </div>

          <ArtifactViewer orgId={orgId} summary={node.output_summary} />

          {steps.length > 0 && (
            <div>
              <p className="mb-1.5 text-[0.7rem] font-medium tracking-wide text-ink-400">
                Steps: every model turn and tool call, in order
              </p>
              <div className="space-y-1.5">
                {steps.map((event) => (
                  <StepRow key={event.seq} event={event} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/* ------------------------------------------------------------- data blocks --- */

/**
 * A node's recorded input or output: a `summarise()` envelope, unwrapped.
 * The badge carries what the envelope knows (type, size, truncation); the
 * body shows the preview as data rather than as a nested curiosity.
 */
/**
 * Files a node produced, shown rather than named.
 *
 * A run that says `artifact_id: 5c44d46c…` has told you nothing you can act
 * on. A poster is meant to be looked at and a video is meant to be played, so
 * they are looked at and played here — the artifact endpoint is
 * session-authenticated, so an <img> or <video> pointed at it just works.
 */
function ArtifactViewer({
  orgId,
  summary,
}: {
  orgId: string | null;
  summary: Summary | null;
}) {
  const preview = (summary?.preview ?? {}) as Record<string, unknown>;
  const id =
    typeof preview.artifact_id === "string" ? preview.artifact_id : null;
  const kind =
    typeof preview.content_type === "string" ? preview.content_type : "";
  const name = typeof preview.filename === "string" ? preview.filename : "file";
  const size = typeof preview.size_bytes === "number" ? preview.size_bytes : 0;
  if (!orgId || !id) return null;

  const src = `/api/v1/orgs/${orgId}/artifacts/${id}`;
  const isVideo = kind.startsWith("video/");
  const isImage = kind.startsWith("image/");
  const isAudio = kind.startsWith("audio/");

  return (
    <div className="mt-3">
      <p className="mb-1.5 text-[0.7rem] font-medium tracking-wide text-ink-400">
        {isVideo ? "Video" : isImage ? "Image" : isAudio ? "Narration" : "File"}
        : {name}
        {size > 0 && ` · ${Math.max(1, Math.round(size / 1024))} KB`}
      </p>
      <div className="overflow-hidden rounded-lg border border-ink-700/70 bg-ink-950/60">
        {isVideo ? (
          // controls + no autoplay: a run page that starts playing sound at
          // you while you are reading a log is a page people close.
          <video
            src={src}
            controls
            preload="metadata"
            className="block max-h-[420px] w-full"
          />
        ) : isImage ? (
          <img
            src={src}
            alt={name}
            className="block max-h-[420px] w-full object-contain"
          />
        ) : isAudio ? (
          // A voice-over is judged by ear and nothing else, so the run page has
          // to be where you can hear it — the alternative is downloading a file
          // to check whether the narration is any good.
          <audio
            src={src}
            controls
            preload="metadata"
            className="block w-full p-3"
          />
        ) : (
          <a
            href={src}
            target="_blank"
            rel="noreferrer"
            className="block p-3 text-xs text-brand-400 underline decoration-dotted"
          >
            Download {name}
          </a>
        )}
      </div>
    </div>
  );
}

function SummaryBlock({
  title,
  summary,
}: {
  title: string;
  summary: Summary | null;
}) {
  if (summary === null) {
    return <JsonBlock title={title} value={null} empty="Nothing recorded." />;
  }

  const badgeParts: string[] = [summary.kind];
  if (typeof summary.keys === "number") badgeParts.push(`${summary.keys} keys`);
  if (typeof summary.length === "number")
    badgeParts.push(`${summary.length.toLocaleString()} long`);

  const body = summary.preview !== undefined ? summary.preview : summary.value;
  const truncated =
    typeof summary.length === "number" &&
    typeof body === "string" &&
    body.length < summary.length;

  return (
    <JsonBlock
      title={title}
      badge={badgeParts.join(" · ")}
      value={body}
      footnote={
        truncated
          ? "Preview truncated. The full value was larger than the log keeps."
          : undefined
      }
    />
  );
}

function JsonBlock({
  title,
  badge,
  value,
  empty = "-",
  footnote,
}: {
  title: string;
  badge?: string;
  value: unknown;
  empty?: string;
  footnote?: string;
}) {
  const [copied, setCopied] = useState(false);
  const isEmpty =
    value === null ||
    value === undefined ||
    (typeof value === "object" &&
      value !== null &&
      Object.keys(value).length === 0);
  const text = isEmpty
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);

  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-baseline gap-2">
        <p className="text-[0.7rem] font-medium tracking-wide text-ink-400">
          {title}
        </p>
        {badge && (
          <span className="font-mono text-[0.7rem] text-ink-500">{badge}</span>
        )}
        {!isEmpty && (
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard?.writeText(text);
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            }}
            className="ml-auto text-[0.7rem] text-ink-500 underline decoration-dotted underline-offset-2 hover:text-ink-200"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>
      {isEmpty ? (
        <p className="rounded-lg border border-ink-700/50 bg-ink-950/40 px-3 py-2.5 text-xs text-ink-600">
          {empty}
        </p>
      ) : (
        <pre className="max-h-72 overflow-auto rounded-lg border border-ink-700/50 bg-ink-950/40 p-3 font-mono text-[0.7rem] leading-relaxed whitespace-pre-wrap text-ink-200">
          {text}
        </pre>
      )}
      {footnote && (
        <p className="mt-1 text-[0.7rem] text-ink-500">{footnote}</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- steps --- */

const STEP_LABEL: Record<
  string,
  { label: string; tone: "good" | "warn" | "bad" | "neutral" }
> = {
  "agent.started": { label: "Agent started", tone: "neutral" },
  "llm.response": { label: "Model call", tone: "neutral" },
  "tool.called": { label: "Tool called", tone: "neutral" },
  "tool.result": { label: "Tool result", tone: "good" },
  "agent.finished": { label: "Agent finished", tone: "good" },
  "agent.truncated": { label: "Agent stopped early", tone: "warn" },
};

function StepRow({ event }: { event: RunEvent }) {
  const step = String(event.data.step ?? "");
  const meta = STEP_LABEL[step] ?? {
    label: step || event.type,
    tone: "neutral" as const,
  };
  const data = event.data;

  return (
    <div className="rounded-lg bg-ink-950/40 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {meta.tone === "neutral" ? (
          <span className="font-medium text-ink-300">{meta.label}</span>
        ) : (
          <StatusPip tone={meta.tone === "bad" ? "bad" : meta.tone}>
            {meta.label}
          </StatusPip>
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
            <span
              className={data.ok ? "text-ink-500" : ""}
              style={data.ok ? undefined : { color: "var(--status-bad)" }}
            >
              {data.ok ? "ok" : "failed"}
            </span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-500">
                {duration(data.duration_ms)}
              </span>
            )}
          </>
        )}
        {step === "llm.response" && (
          <>
            <span className="font-mono text-ink-400">
              {String(data.model ?? "")}
            </span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-500">
                {duration(data.duration_ms)}
              </span>
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
              <span className="text-ink-500">
                {data.tool_calls} tool call(s)
              </span>
            )}
            {typeof data.cost_usd === "number" && (
              <span className="font-mono text-ink-200">
                ${Number(data.cost_usd).toFixed(4)}
              </span>
            )}
          </>
        )}

        <span className="ml-auto shrink-0 text-ink-600">
          <RelativeTime value={event.at} />
        </span>
      </div>

      {step === "tool.result" && typeof data.result_preview === "string" && (
        <p className="mt-1 truncate font-mono text-ink-500">
          {data.result_preview}
        </p>
      )}
      {step === "llm.response" &&
        typeof data.text_preview === "string" &&
        data.text_preview && (
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

/* ----------------------------------------------------------------- labels --- */

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
      <span
        className={`text-ink-200 ${mono ? "font-mono" : ""} ${capitalize ? "capitalize" : ""}`}
      >
        {value}
      </span>
    </span>
  );
}

function RunStatusLabel({ status }: { status: RunStatus }) {
  if (status === "succeeded")
    return <StatusPip tone="good">Succeeded</StatusPip>;
  if (status === "failed") return <StatusPip tone="bad">Failed</StatusPip>;
  if (status === "cancelled")
    return <StatusPip tone="warn">Cancelled</StatusPip>;
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
  if (status === "succeeded")
    return <StatusPip tone="good">Succeeded</StatusPip>;
  if (status === "failed") return <StatusPip tone="bad">Failed</StatusPip>;
  if (status === "skipped") return <StatusPip tone="warn">Skipped</StatusPip>;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-300">
      <span className="h-2 w-2 rounded-full bg-brand-400" />
      {status === "running" ? "Running" : "Pending"}
    </span>
  );
}
