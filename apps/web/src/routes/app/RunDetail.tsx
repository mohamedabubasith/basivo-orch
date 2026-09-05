/**
 * One run, in full: every node execution, and every step inside it.
 *
 * Three layers, from coarse to fine: the run (its input and final output,
 * verbatim), each node (status, attempt, duration, tokens, cost, and its
 * recorded input and output, which is the part every other tool hides), and
 * inside an agent node, every model turn and tool call as ordered steps.
 *
 * Node input/output arrive as `summarise()` envelopes, `{kind, preview,
 * keys|length}`, because payloads can be megabytes and the log table must not
 * become the biggest thing in the database. The UI unwraps the envelope: the
 * preview is shown as data, the envelope becomes a badge ("object · 14 keys"),
 * and truncation is said out loud rather than passed off as the whole value.
 *
 * Failed nodes arrive pre-expanded: the person opening this page at 3am came
 * to read exactly that one.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { NodeIcon, nodeAccent } from "../../builder/nodeIcons";
import {
  Alert,
  Button,
  Card,
  IconChip,
  PageLoader,
  Pill,
  Section,
  type Tone,
} from "../../components/ui";
import { api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { RelativeTime, duration } from "./bits";

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
  status: string;
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
  status: string;
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

type StatusMeta = { label: string; tone: Tone };

/** The engine says `queued`; older rows may say `pending`. */
const RUN_STATUS: Record<string, StatusMeta> = {
  queued: { label: "Queued", tone: "warn" },
  pending: { label: "Queued", tone: "warn" },
  running: { label: "Running", tone: "warn" },
  succeeded: { label: "Succeeded", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

const NODE_STATUS: Record<string, StatusMeta> = {
  pending: { label: "Pending", tone: "warn" },
  running: { label: "Running", tone: "warn" },
  succeeded: { label: "Succeeded", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  skipped: { label: "Skipped", tone: "neutral" },
};

const TRIGGER_LABEL: Record<string, string> = {
  manual: "Manual",
  webhook: "Webhook",
  schedule: "Scheduled",
  api: "API",
  telegram: "Telegram",
};

const TERMINAL = ["succeeded", "failed", "cancelled"];

function money(value: number): string {
  return `$${value.toFixed(value >= 1 ? 2 : 4)}`;
}

const PAYLOAD_GLYPH = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9.5 4.5C7.5 4.5 7 5.5 7 7v2.5c0 1.2-.8 2.5-2.5 2.5C6.2 12 7 13.3 7 14.5V17c0 1.5.5 2.5 2.5 2.5" />
    <path d="M14.5 4.5c2 0 2.5 1 2.5 2.5v2.5c0 1.2.8 2.5 2.5 2.5-1.7 0-2.5 1.3-2.5 2.5V17c0 1.5-.5 2.5-2.5 2.5" />
  </svg>
);

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const { orgId } = useWorkspace();
  const navigate = useNavigate();
  const [run, setRun] = useState<RunDetailResponse | null>(null);
  const [events, setEvents] = useState<RunEvent[] | null>(null);
  const [flowName, setFlowName] = useState<string | null>(null);
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
    if (!run || TERMINAL.includes(run.status)) return;
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [run, load]);

  // The run carries its flow's id, not its name. One fetch, keyed on the flow
  // rather than on every poll.
  const flowId = run?.flow_id;
  useEffect(() => {
    if (!orgId || !flowId) return;
    void api
      .get<{ name: string }>(`/api/v1/orgs/${orgId}/flows/${flowId}`)
      .then((flow) => setFlowName(flow.name))
      .catch(() => setFlowName(null));
  }, [orgId, flowId]);

  if (error) {
    return (
      <div className="space-y-4">
        <BackLink />
        <Alert>{error}</Alert>
      </div>
    );
  }
  if (!run || events === null) return <PageLoader label="Loading run" />;

  const hasCost = run.nodes.some((node) => node.cost_usd !== null);
  const cost = run.nodes.reduce((sum, node) => sum + (node.cost_usd ?? 0), 0);
  const nodeCount = run.nodes.length;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <BackLink />
          <div className="mt-1.5 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight text-ink-100">
              {flowName ?? "Run"}
            </h1>
            <StatusPill status={run.status} table={RUN_STATUS} />
          </div>
        </div>
        <Button
          variant="secondary"
          onClick={() => navigate(`/app/flows/${run.flow_id}`)}
        >
          Open flow
        </Button>
      </header>

      <Card className="px-5 py-4">
        <dl className="flex flex-wrap gap-x-8 gap-y-4">
          <Fact label="Started">
            <RelativeTime value={run.started_at ?? run.created_at} />
          </Fact>
          <Fact label="Duration" mono>
            {duration(run.duration_ms)}
          </Fact>
          <Fact label="Trigger">
            {TRIGGER_LABEL[run.trigger] ?? run.trigger}
          </Fact>
          {hasCost && (
            <Fact label="Cost" mono dim={cost === 0}>
              {money(cost)}
            </Fact>
          )}
          <Fact label="Run id" mono>
            <span className="inline-flex items-center gap-1.5">
              <span className="wrap-anywhere">{run.id}</span>
              <CopyButton value={run.id} label="Copy run id" />
            </span>
          </Fact>
        </dl>
      </Card>

      {run.error && (
        <Alert>
          <span className="font-mono text-xs whitespace-pre-wrap wrap-anywhere">
            {run.error}
          </span>
        </Alert>
      )}

      {/* The run's own boundary values, verbatim, not summaries. */}
      <Section
        icon={PAYLOAD_GLYPH}
        title="Payload"
        description="What the trigger received and what the last node returned."
      >
        <div className="mt-5 grid gap-4 lg:grid-cols-2">
          <JsonBlock title="Run input" value={run.input} />
          <JsonBlock
            title="Final output"
            value={run.output}
            empty="No output. The run did not finish."
          />
        </div>
      </Section>

      <section>
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 className="text-base font-semibold text-ink-100">Timeline</h2>
          <p className="text-sm text-ink-400">
            {nodeCount} node execution{nodeCount === 1 ? "" : "s"}
          </p>
        </div>
        {nodeCount === 0 ? (
          <Card className="px-5 py-8 text-center text-sm text-ink-400">
            No node has started yet.
          </Card>
        ) : (
          <ol className="space-y-2">
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
          </ol>
        )}
      </section>
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
  /** Needed to build artifact URLs; files are tenant-scoped. */
  orgId: string | null;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const steps = events.filter(
    (event) =>
      event.data.node_id === node.node_id && event.type === "node.step",
  );

  const facts = [node.node_type];
  if (node.tokens_in !== null) {
    facts.push(
      `${node.tokens_in.toLocaleString()}→${(node.tokens_out ?? 0).toLocaleString()} tokens`,
    );
  }
  if (node.cost_usd !== null) facts.push(money(node.cost_usd));

  return (
    <li className="surface overflow-hidden rounded-2xl">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-3.5 px-4 py-3.5 text-left transition-colors hover:bg-ink-800/40"
      >
        <IconChip hue={nodeAccent(node.node_type)}>
          <NodeIcon type={node.node_type} />
        </IconChip>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <span className="max-w-full min-w-0 truncate font-medium text-ink-100">
              {node.node_name ?? node.node_id}
            </span>
            <StatusPill status={node.status} table={NODE_STATUS} />
            {node.attempt > 1 && (
              <Pill tone="warn">Attempt {node.attempt}</Pill>
            )}
          </div>
          <p className="mt-0.5 truncate font-mono text-xs text-ink-400">
            {facts.join(" · ")}
          </p>
        </div>
        <span className="flex-none font-mono text-xs text-ink-300 tabular-nums">
          {duration(node.duration_ms)}
        </span>
        <svg
          viewBox="0 0 24 24"
          className={cx(
            "h-4 w-4 flex-none text-ink-400 transition-transform",
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
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div className="space-y-4 border-t border-[var(--edge)] px-4 py-4">
          {node.error && (
            <Alert>
              <span className="font-mono text-xs whitespace-pre-wrap wrap-anywhere">
                {node.error}
              </span>
            </Alert>
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <SummaryBlock title="Input" summary={node.input_summary} />
            <SummaryBlock title="Output" summary={node.output_summary} />
          </div>

          <ArtifactViewer orgId={orgId} summary={node.output_summary} />

          {steps.length > 0 && (
            <div>
              <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <p className="text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
                  Agent steps
                </p>
                <p className="text-xs text-ink-400">
                  Every model turn and tool call, in order.
                </p>
              </div>
              <ol className="space-y-1.5">
                {steps.map((event) => (
                  <StepRow key={event.seq} event={event} />
                ))}
              </ol>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/* ------------------------------------------------------------- data blocks --- */

/**
 * Files a node produced, shown rather than named.
 *
 * A run that says `artifact_id: 5c44d46c…` has told you nothing you can act
 * on. A poster is meant to be looked at and a video is meant to be played, so
 * they are looked at and played here. The artifact endpoint is
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
    <div>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <p className="text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
          {isVideo ? "Video" : isImage ? "Image" : isAudio ? "Narration" : "File"}
        </p>
        <p className="font-mono text-xs text-ink-300">
          {name}
          {size > 0 && ` · ${Math.max(1, Math.round(size / 1024))} KB`}
        </p>
      </div>
      <div className="overflow-hidden rounded-xl border border-[var(--edge)] bg-ink-900/60">
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
          // A voice-over is judged by ear and nothing else, so the run page
          // has to be where you can hear it.
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
            className="block p-3 text-xs font-medium text-brand-300 hover:underline"
          >
            Download {name}
          </a>
        )}
      </div>
    </div>
  );
}

/**
 * A node's recorded input or output: a `summarise()` envelope, unwrapped.
 * The badge carries what the envelope knows (type, size, truncation); the
 * body shows the preview as data rather than as a nested curiosity.
 */
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
  empty = "Empty.",
  footnote,
}: {
  title: string;
  badge?: string;
  value: unknown;
  empty?: string;
  footnote?: string;
}) {
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
      <div className="mb-1.5 flex items-center gap-2">
        <p className="text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
          {title}
        </p>
        {badge && (
          <span className="font-mono text-xs text-ink-400">{badge}</span>
        )}
        {!isEmpty && (
          <span className="ml-auto">
            <CopyButton value={text} label={`Copy ${title.toLowerCase()}`} />
          </span>
        )}
      </div>
      {isEmpty ? (
        <p className="rounded-xl border border-dashed border-[var(--edge-strong)] px-3.5 py-3 text-xs text-ink-400">
          {empty}
        </p>
      ) : (
        // Scrolls inside its own box, and wraps anywhere: a long token must
        // never set the width of the page.
        <pre className="max-h-72 overflow-auto rounded-xl border border-[var(--edge)] bg-ink-900/60 p-3.5 font-mono text-xs leading-relaxed whitespace-pre-wrap wrap-anywhere text-ink-200">
          {text}
        </pre>
      )}
      {footnote && <p className="mt-1.5 text-xs text-ink-400">{footnote}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------- steps --- */

const STEP_LABEL: Record<string, StatusMeta> = {
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
    tone: "neutral" as Tone,
  };
  const data = event.data;

  return (
    <li className="rounded-xl bg-ink-900/60 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {meta.tone === "neutral" ? (
          <span className="font-medium text-ink-200">{meta.label}</span>
        ) : (
          <Pill tone={meta.tone}>{meta.label}</Pill>
        )}

        {step === "tool.called" && (
          <>
            <span className="font-mono text-ink-100">{String(data.tool)}</span>
            <span className="min-w-0 flex-1 truncate text-ink-400">
              {JSON.stringify(data.arguments).slice(0, 80)}
            </span>
          </>
        )}
        {step === "tool.result" && (
          <>
            <span className="font-mono text-ink-100">{String(data.tool)}</span>
            <span
              className={data.ok ? "text-ink-400" : "font-medium"}
              style={data.ok ? undefined : { color: "var(--status-bad)" }}
            >
              {data.ok ? "ok" : "failed"}
            </span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-400">
                {duration(data.duration_ms)}
              </span>
            )}
          </>
        )}
        {step === "llm.response" && (
          <>
            <span className="font-mono text-ink-300">
              {String(data.model ?? "")}
            </span>
            {typeof data.duration_ms === "number" && (
              <span className="font-mono text-ink-400">
                {duration(data.duration_ms)}
              </span>
            )}
            {typeof data.input_tokens === "number" && (
              <span className="font-mono text-ink-400">
                {data.input_tokens}→{Number(data.output_tokens ?? 0)} tokens
              </span>
            )}
          </>
        )}
        {step === "agent.finished" && (
          <>
            {typeof data.tool_calls === "number" && (
              <span className="text-ink-400">
                {data.tool_calls} tool call{data.tool_calls === 1 ? "" : "s"}
              </span>
            )}
            {typeof data.cost_usd === "number" && (
              <span className="font-mono text-ink-100">
                {money(Number(data.cost_usd))}
              </span>
            )}
          </>
        )}

        <span className="ml-auto shrink-0 text-ink-400">
          <RelativeTime value={event.at} />
        </span>
      </div>

      {step === "tool.result" && typeof data.result_preview === "string" && (
        <p className="mt-1 truncate font-mono text-ink-400">
          {data.result_preview}
        </p>
      )}
      {step === "llm.response" &&
        typeof data.text_preview === "string" &&
        data.text_preview && (
          <p className="mt-1 truncate text-ink-400">{data.text_preview}</p>
        )}
      {step === "agent.truncated" && typeof data.reason === "string" && (
        <p className="mt-1" style={{ color: "var(--status-warn)" }}>
          {data.reason}
        </p>
      )}
    </li>
  );
}

/* ------------------------------------------------------------------ pieces --- */

function BackLink() {
  return (
    <Link
      to="/app/runs"
      className="inline-flex items-center gap-1 text-xs font-medium tracking-[0.14em] text-brand-400 uppercase transition-colors hover:text-brand-300"
    >
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" aria-hidden="true">
        <path
          d="M14 7l-5 5 5 5"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      All runs
    </Link>
  );
}

function StatusPill({
  status,
  table,
}: {
  status: string;
  table: Record<string, StatusMeta>;
}) {
  const meta = table[status] ?? { label: status, tone: "neutral" as Tone };
  return (
    <Pill
      tone={meta.tone}
      dot
      className={status === "running" ? "[&>span]:animate-pulse" : undefined}
    >
      {meta.label}
    </Pill>
  );
}

function Fact({
  label,
  mono,
  dim,
  children,
}: {
  label: string;
  mono?: boolean;
  /** For a value that is technically present but says nothing, like a cost of zero. */
  dim?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
        {label}
      </dt>
      <dd
        className={cx(
          "mt-1 text-sm",
          dim ? "text-ink-400" : "text-ink-100",
          mono && "font-mono tabular-nums",
        )}
      >
        {children}
      </dd>
    </div>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      onClick={() => {
        void navigator.clipboard?.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className="flex-none rounded-md p-1 text-ink-400 transition-colors hover:bg-ink-800 hover:text-ink-100"
    >
      <svg
        viewBox="0 0 24 24"
        className="h-3.5 w-3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {copied ? (
          <path d="M5 12.5 10 17.5 19 7" style={{ stroke: "var(--status-good)" }} />
        ) : (
          <>
            <rect x="9" y="9" width="11" height="11" rx="2" />
            <path d="M5 15V6a2 2 0 0 1 2-2h9" />
          </>
        )}
      </svg>
    </button>
  );
}
