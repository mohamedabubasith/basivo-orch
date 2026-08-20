/**
 * The flow list.
 *
 * A flow is either published or it is a draft, and the difference is the whole
 * story of the row: only a published flow can be triggered by anything outside
 * this screen. So that state is the first thing shown, in words, not a colour a
 * reader has to learn.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Card, Field, PageLoader } from "../../components/ui";
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

/** What starts a flow, in the words a person would use. */
const TRIGGER_LABEL: Record<string, string> = {
  "trigger.manual": "Manual",
  "trigger.webhook": "Webhook",
  "trigger.schedule": "Scheduled",
};

const RUN_TONE: Record<string, "good" | "bad" | "warn"> = {
  succeeded: "good",
  failed: "bad",
  running: "warn",
  queued: "warn",
  cancelled: "warn",
};

export function Flows() {
  const { orgId } = useWorkspace();
  const [flows, setFlows] = useState<Flow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
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

  async function remove(flow: Flow) {
    if (
      !confirm(
        `Delete "${flow.name}"? Its run history goes with it, and anything calling its published URL starts getting 404s. This cannot be undone.`,
      )
    )
      return;
    try {
      await api.del(`/api/v1/orgs/${orgId}/flows/${flow.id}`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete that flow.");
    }
  }

  if (flows === null) return <PageLoader label="Loading flows" />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Build"
        title="Flows"
        subtitle="A flow is a graph of nodes. Publish one to give it a stable version other systems can call."
        action={
          <Button onClick={() => setCreating((value) => !value)}>
            {creating ? "Cancel" : "New flow"}
          </Button>
        }
      />

      {error && <Alert>{error}</Alert>}

      {creating && (
        <NewFlow orgId={orgId!} onCreated={(id) => navigate(`/app/flows/${id}`)} />
      )}

      {flows.length === 0 && !creating ? (
        <Card className="p-10 text-center">
          <p className="text-ink-200">No flows yet.</p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-500">
            A flow starts as a draft you can edit freely. Publishing freezes a
            version and gives it a URL, so a change here can never alter what a
            caller in production is already running.
          </p>
          <div className="mt-5">
            <Button onClick={() => setCreating(true)}>Create your first flow</Button>
          </div>
        </Card>
      ) : (
        <ul className="overflow-hidden rounded-2xl border border-ink-800/70">
          {flows.map((flow, index) => {
            const published = Boolean(flow.published_version_id);
            const tone = flow.last_run_status ? RUN_TONE[flow.last_run_status] : null;
            return (
              <motion.li
                key={flow.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(index * 0.02, 0.2), duration: 0.2 }}
                // One list with hairline dividers, not a stack of floating
                // cards: eleven cards at 130px each is two screens of scroll
                // for information that fits in one.
                className="group relative border-b border-ink-800/70 bg-ink-900/30 transition-colors last:border-b-0 hover:bg-ink-800/40"
              >
                {/* Published or draft, readable before any label is: the same
                    rail device as the node cards and the stat tiles. */}
                <span
                  aria-hidden="true"
                  className="absolute inset-y-0 left-0 w-[3px]"
                  style={{
                    background: published ? "var(--status-good)" : "var(--status-warn)",
                    opacity: published ? 0.9 : 0.55,
                  }}
                />
                <Link
                  to={`/app/flows/${flow.id}`}
                  className="flex min-w-0 items-center gap-4 py-3 pr-14 pl-5"
                >
                  <div className="min-w-0 flex-[2]">
                    <div className="flex items-baseline gap-2">
                      <p className="truncate text-[0.95rem] font-medium text-ink-100">
                        {flow.name}
                      </p>
                      {!published && (
                        <span className="flex-none text-[0.68rem] text-[var(--status-warn)]">
                          Draft
                        </span>
                      )}
                    </div>
                    {/* The slug identifies it to other systems; "no
                        description" repeated down a page is noise, so it is
                        simply absent. */}
                    <p className="mt-0.5 truncate font-mono text-[0.68rem] text-ink-500">
                      {flow.slug}
                    </p>
                  </div>

                  {/* The three facts the page is opened to find. */}
                  <div className="hidden flex-1 items-center gap-5 text-xs text-ink-400 sm:flex">
                    <span className="w-20 flex-none">
                      {flow.trigger_type ? TRIGGER_LABEL[flow.trigger_type] ?? "Trigger" : "—"}
                    </span>
                    <span className="w-16 flex-none [font-variant-numeric:tabular-nums]">
                      {flow.node_count === 0
                        ? "empty"
                        : `${flow.node_count} node${flow.node_count > 1 ? "s" : ""}`}
                    </span>
                    <span className="min-w-0 flex-1 truncate">
                      {flow.last_run_status && tone ? (
                        <span className="inline-flex items-center gap-1.5">
                          <span
                            className="h-1.5 w-1.5 flex-none rounded-full"
                            style={{
                              background:
                                tone === "good"
                                  ? "var(--status-good)"
                                  : tone === "bad"
                                    ? "var(--status-bad)"
                                    : "var(--status-warn)",
                            }}
                            aria-hidden="true"
                          />
                          {flow.last_run_status}
                          {flow.last_run_at && (
                            <span className="text-ink-500">
                              <RelativeTime value={flow.last_run_at} />
                            </span>
                          )}
                        </span>
                      ) : (
                        <span className="text-ink-600">never run</span>
                      )}
                    </span>
                  </div>
                </Link>

                {/* Revealed on hover rather than a permanent bordered box on
                    every row: destructive controls should not be the loudest
                    thing on a list. Focus shows it too, so it stays reachable
                    from the keyboard. */}
                <button
                  onClick={() => void remove(flow)}
                  aria-label={`Delete flow ${flow.name}`}
                  title="Delete this flow"
                  className="absolute top-1/2 right-4 -translate-y-1/2 rounded-lg p-2 text-ink-500 opacity-0 transition-all group-hover:opacity-100 hover:bg-ink-800 hover:text-[var(--status-bad)] focus-visible:opacity-100"
                >
                  <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden="true">
                    <path
                      d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M7 6.5l.8 12a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-12M10 10.5v6M14 10.5v6"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </motion.li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function NewFlow({ orgId, onCreated }: { orgId: string; onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<{ id: string }>(`/api/v1/orgs/${orgId}/flows`, {
        name: name.trim(),
        description: description.trim() || null,
      });
      // Straight into the canvas. The list has nothing more to say about a
      // flow that was created one second ago.
      onCreated(created.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the flow.");
      setBusy(false);
    }
  }

  return (
    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}>
      <Card className="p-6">
        <form onSubmit={submit} className="max-w-md space-y-4" noValidate>
          {error && <Alert>{error}</Alert>}
          <Field
            label="Name"
            name="name"
            required
            autoFocus
            placeholder="Nightly digest"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <Field
            label="Description"
            name="description"
            placeholder="Optional — what it does and who depends on it"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <Button type="submit" loading={busy} disabled={!name.trim()}>
            Create flow
          </Button>
        </form>
      </Card>
    </motion.div>
  );
}
