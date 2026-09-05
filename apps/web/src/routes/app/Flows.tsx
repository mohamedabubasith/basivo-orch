/**
 * The flow list.
 *
 * A flow is either published or it is a draft, and the difference is the whole
 * story of the row: only a published flow can be triggered by anything outside
 * this screen. So that state is the first thing shown, in words, not a colour a
 * reader has to learn.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { NodeIcon } from "../../builder/nodeIcons";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  IconChip,
  Modal,
  PageLoader,
  Pill,
  type Tone,
} from "../../components/ui";
import { ApiError, api } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { PageHeader, RelativeTime } from "./bits";

interface Flow {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  published_version_id: string | null;
  created_at: string;
  updated_at: string;
  node_count: number;
  trigger_type: string | null;
  last_run_status: string | null;
  last_run_at: string | null;
  next_run_at: string | null;
}

/**
 * What starts a flow: the word and the hue, one row per kind. Manual, webhook
 * and schedule borrow the canvas node's glyph so the list and the builder
 * agree; Telegram has no canvas icon yet, so it carries its own.
 */
const TRIGGERS: Record<string, { label: string; hue: string; glyph?: ReactNode }> =
  {
    "trigger.manual": { label: "Manual", hue: "var(--color-brand-400)" },
    "trigger.webhook": { label: "Webhook", hue: "var(--color-accent-500)" },
    "trigger.schedule": { label: "Scheduled", hue: "var(--status-warn)" },
    "trigger.telegram": {
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

function triggerLabel(type: string | null): string {
  if (!type) return "No trigger";
  return TRIGGERS[type]?.label ?? "Trigger";
}

function TriggerChip({
  type,
  className,
}: {
  type: string | null;
  className?: string;
}) {
  if (!type) {
    return (
      <IconChip hue="var(--color-ink-400)" className={className}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
          <circle cx="12" cy="12" r="7.5" strokeDasharray="3 3" />
        </svg>
      </IconChip>
    );
  }
  const spec = TRIGGERS[type];
  return (
    <IconChip hue={spec?.hue ?? "var(--series)"} className={className}>
      {spec?.glyph ?? <NodeIcon type={type} />}
    </IconChip>
  );
}

const RUN_STATUS: Record<string, { label: string; tone: Tone }> = {
  succeeded: { label: "Succeeded", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
  running: { label: "Running", tone: "warn" },
  queued: { label: "Queued", tone: "warn" },
  pending: { label: "Queued", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "warn" },
};

type Filter = "all" | "published" | "drafts";

const SEGMENTS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "published", label: "Published" },
  { key: "drafts", label: "Drafts" },
];

const FLOW_GLYPH = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="6" cy="6" r="2.5" />
    <circle cx="18" cy="6" r="2.5" />
    <circle cx="12" cy="18" r="2.5" />
    <path d="M6 8.5v2a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-2M12 13.5v2" />
  </svg>
);

export function Flows() {
  const { orgId } = useWorkspace();
  const [flows, setFlows] = useState<Flow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  /** The flow whose deletion is awaiting confirmation. */
  const [pending, setPending] = useState<Flow | null>(null);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setFlows(await api.get<Flow[]>(`/api/v1/orgs/${orgId}/flows`));
      setError(null);
    } catch {
      setError("Could not load your flows.");
      setFlows([]);
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function destroy() {
    if (!pending) return;
    setDeleting(true);
    try {
      await api.del(`/api/v1/orgs/${orgId}/flows/${pending.id}`);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete that flow.",
      );
    } finally {
      setDeleting(false);
      setPending(null);
    }
  }

  if (flows === null) return <PageLoader label="Loading flows" />;

  /**
   * One click, straight into the canvas.
   *
   * It used to open a form asking for a name and a description before anything
   * existed — two fields standing between an idea and a canvas, filled in
   * before there is anything to describe. Every tool of this kind does it the
   * other way round: make the thing, then name it once you know what it is. The
   * title in the builder header is editable, and `?new=1` puts the cursor in
   * it.
   */
  async function create() {
    setCreating(true);
    setError(null);
    try {
      const made = await api.post<{ id: string }>(
        `/api/v1/orgs/${orgId}/flows`,
        {
          name: "Untitled flow",
        },
      );
      navigate(`/app/flows/${made.id}?new=1`);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create the flow.",
      );
      setCreating(false);
    }
  }

  const publishedCount = flows.filter((f) => f.published_version_id).length;
  const counts: Record<Filter, number> = {
    all: flows.length,
    published: publishedCount,
    drafts: flows.length - publishedCount,
  };
  const needle = query.trim().toLowerCase();
  const visible = flows.filter((flow) => {
    const isPublished = Boolean(flow.published_version_id);
    if (filter === "published" && !isPublished) return false;
    if (filter === "drafts" && isPublished) return false;
    return (
      !needle ||
      flow.name.toLowerCase().includes(needle) ||
      flow.slug.toLowerCase().includes(needle)
    );
  });

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Build"
        title="Flows"
        subtitle="A flow is a graph of nodes. Publish one to give it a stable version other systems can call."
        action={
          <Button onClick={() => void create()} loading={creating}>
            New flow
          </Button>
        }
      />

      {error && <Alert>{error}</Alert>}

      {flows.length === 0 ? (
        <EmptyState
          icon={FLOW_GLYPH}
          title="Build your first flow"
          action={
            <Button onClick={() => void create()} loading={creating}>
              New flow
            </Button>
          }
        >
          A flow starts as a draft you can shape freely on the canvas. Publish it
          when it is ready and it gets a stable version other systems can call,
          so a later edit never changes what production is running.
        </EmptyState>
      ) : (
        <>
          {/* Search and filter only once there is enough to lose something in. */}
          {flows.length >= 5 && (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <label className="relative block w-full sm:w-72">
                <span className="sr-only">Search flows</span>
                <svg
                  viewBox="0 0 24 24"
                  className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-ink-400"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <circle cx="11" cy="11" r="6.5" />
                  <path d="M20 20l-4.2-4.2" />
                </svg>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search by name or slug"
                  className={cx(
                    "w-full rounded-xl border border-ink-600/70 bg-ink-900/70 py-2 pr-3 pl-9 text-sm text-ink-100",
                    "placeholder:text-ink-400 transition-all duration-150 hover:border-ink-500",
                    "focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none",
                  )}
                />
              </label>

              <div
                role="radiogroup"
                aria-label="Filter flows"
                className="flex items-center gap-0.5 rounded-xl border border-ink-700/60 bg-ink-900/50 p-0.5"
              >
                {SEGMENTS.map((segment) => {
                  const active = filter === segment.key;
                  return (
                    <button
                      key={segment.key}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => setFilter(segment.key)}
                      className={cx(
                        "relative flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
                        active
                          ? "text-ink-100"
                          : "text-ink-300 hover:text-ink-100",
                      )}
                    >
                      {active && (
                        <motion.span
                          layoutId="flows-filter-pill"
                          className="absolute inset-0 rounded-lg bg-ink-800"
                          transition={{
                            type: "spring",
                            stiffness: 400,
                            damping: 32,
                          }}
                        />
                      )}
                      <span className="relative">{segment.label}</span>
                      <span className="relative text-xs text-ink-400 tabular-nums">
                        {counts[segment.key]}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Scroll rather than clip: a table cannot shrink below its content,
              and a clipped actions column is an unreachable delete button. */}
          <Card className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[var(--edge)] bg-ink-900/40 text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
                  <th scope="col" className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
                    Flow
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Trigger
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Nodes
                  </th>
                  <th scope="col" className="px-3 py-3 md:px-4">
                    Last run
                  </th>
                  <th scope="col" className="hidden px-4 py-3 md:table-cell">
                    Updated
                  </th>
                  <th scope="col" className="py-3 pr-2 pl-1 md:pr-4 md:pl-4">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((flow, index) => {
                  const isPublished = Boolean(flow.published_version_id);
                  const run = flow.last_run_status
                    ? (RUN_STATUS[flow.last_run_status] ?? {
                        label: flow.last_run_status,
                        tone: "neutral" as Tone,
                      })
                    : null;
                  const nodes =
                    flow.node_count === 0
                      ? "Empty"
                      : `${flow.node_count} node${flow.node_count === 1 ? "" : "s"}`;
                  return (
                    <motion.tr
                      key={flow.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        delay: Math.min(index * 0.02, 0.2),
                        duration: 0.2,
                      }}
                      className="group border-b border-[var(--edge)] transition-colors last:border-b-0 hover:bg-ink-800/40"
                    >
                      {/* Below md the name and slug wrap instead of truncating:
                          nowrap text sets a table cell's minimum width, and
                          that minimum is what pushed the row past a phone. */}
                      <td className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
                        <div className="flex min-w-0 items-center gap-3.5">
                          <TriggerChip
                            type={flow.trigger_type}
                            className="max-sm:hidden"
                          />
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                              <Link
                                to={`/app/flows/${flow.id}`}
                                title={flow.name}
                                className="max-w-full min-w-0 font-medium text-ink-100 transition-colors hover:text-brand-300 md:truncate"
                              >
                                {flow.name}
                              </Link>
                              <Pill tone={isPublished ? "good" : "warn"}>
                                {isPublished ? "Published" : "Draft"}
                              </Pill>
                            </div>
                            <p className="mt-0.5 font-mono text-xs text-ink-400 wrap-anywhere md:truncate">
                              {flow.slug}
                            </p>
                            {/* The columns hidden on small screens, folded
                                into the one that stays. */}
                            <p className="mt-1 text-xs text-ink-400 md:hidden">
                              {triggerLabel(flow.trigger_type)} · {nodes}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td
                        className={cx(
                          "hidden px-4 py-3 md:table-cell",
                          flow.trigger_type ? "text-ink-300" : "text-ink-400",
                        )}
                      >
                        {triggerLabel(flow.trigger_type)}
                      </td>
                      <td className="hidden px-4 py-3 text-ink-300 tabular-nums md:table-cell">
                        {nodes}
                      </td>
                      <td className="px-3 py-3 md:px-4">
                        <div className="flex flex-wrap items-center gap-2">
                          {run ? (
                            <Pill tone={run.tone} dot>
                              {run.label}
                            </Pill>
                          ) : (
                            <Pill tone="neutral">Never run</Pill>
                          )}
                          {flow.last_run_at && (
                            <span className="text-xs text-ink-400">
                              <RelativeTime value={flow.last_run_at} />
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="hidden px-4 py-3 whitespace-nowrap text-ink-300 md:table-cell">
                        <RelativeTime value={flow.updated_at} />
                      </td>
                      <td className="py-3 pr-2 pl-1 md:pr-4 md:pl-4">
                        <div className="flex items-center justify-end gap-1.5">
                          {/* Below md the name link is the way in; a second
                              button per row is what pushed the column off
                              a phone screen. */}
                          <span className="hidden md:inline-flex">
                            <Button
                              variant="secondary"
                              onClick={() => navigate(`/app/flows/${flow.id}`)}
                            >
                              Open
                            </Button>
                          </span>
                          {/* Revealed on hover and focus rather than a
                              permanent red control on every row; always shown
                              below md, where there is no hover. */}
                          <button
                            type="button"
                            onClick={() => setPending(flow)}
                            aria-label={`Delete flow ${flow.name}`}
                            title="Delete this flow"
                            className="rounded-lg p-2 text-ink-400 opacity-0 transition-all group-hover:opacity-100 hover:bg-ink-800 hover:text-[var(--status-bad)] focus-visible:opacity-100 max-md:opacity-100"
                          >
                            <svg
                              viewBox="0 0 24 24"
                              className="h-4 w-4"
                              fill="none"
                              aria-hidden="true"
                            >
                              <path
                                d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M7 6.5l.8 12a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-12M10 10.5v6M14 10.5v6"
                                stroke="currentColor"
                                strokeWidth="1.6"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </button>
                        </div>
                      </td>
                    </motion.tr>
                  );
                })}
              </tbody>
            </table>

            {visible.length === 0 && (
              <p className="px-5 py-10 text-center text-sm text-ink-400">
                No flows match{needle ? ` “${query.trim()}”` : " this filter"}.
                <button
                  type="button"
                  onClick={() => {
                    setQuery("");
                    setFilter("all");
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

      {pending && (
        <Modal
          size="sm"
          title={`Delete “${pending.name}”?`}
          description="This cannot be undone."
          onClose={() => {
            if (!deleting) setPending(null);
          }}
          footer={
            <>
              <Button
                variant="ghost"
                onClick={() => setPending(null)}
                disabled={deleting}
              >
                Cancel
              </Button>
              {/* Solid status-bad with ink-950 text: the positional scale
                  flips with the theme, so the text stays legible on both the
                  pale dark-theme rose and the deep light-theme one. */}
              <Button
                onClick={() => void destroy()}
                loading={deleting}
                className="hover:brightness-110"
                style={{
                  background: "var(--status-bad)",
                  color: "var(--color-ink-950)",
                }}
              >
                Delete flow
              </Button>
            </>
          }
        >
          <ul className="list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-ink-300">
            <li>Its run history is deleted with it.</li>
            {pending.published_version_id ? (
              <li>
                Anything calling its published URL starts getting 404s the
                moment it is gone.
              </li>
            ) : (
              <li>
                It was never published, so nothing outside this workspace
                depends on it.
              </li>
            )}
          </ul>
        </Modal>
      )}
    </div>
  );
}
