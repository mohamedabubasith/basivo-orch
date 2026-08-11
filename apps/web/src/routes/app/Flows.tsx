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

import { ApiError, api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Card, Field, PageLoader } from "../../components/ui";
import { StatusPip } from "../../components/charts";
import { PageHeader, RelativeTime } from "./bits";

interface Flow {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  published_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export function Flows() {
  const { orgId } = useWorkspace();
  const [flows, setFlows] = useState<Flow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

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

  if (flows === null) return <PageLoader label="Loading flows" />;

  return (
    <div className="space-y-6">
      <PageHeader
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
        <NewFlow
          orgId={orgId!}
          onCreated={() => {
            setCreating(false);
            void load();
          }}
        />
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
        <ul className="space-y-2.5">
          {flows.map((flow, index) => (
            <motion.li
              key={flow.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(index * 0.03, 0.25), duration: 0.25 }}
            >
              <div className="surface flex flex-wrap items-center gap-4 rounded-2xl p-5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-3">
                    <p className="truncate font-medium text-ink-100">{flow.name}</p>
                    {flow.published_version_id ? (
                      <StatusPip tone="good">Published</StatusPip>
                    ) : (
                      <StatusPip tone="warn">Draft</StatusPip>
                    )}
                  </div>
                  <p className="mt-1 truncate text-sm text-ink-400">
                    {flow.description || <span className="text-ink-600">No description</span>}
                  </p>
                </div>
                <div className="text-right text-xs text-ink-500">
                  <p className="font-mono">{flow.slug}</p>
                  <p className="mt-1">
                    Updated <RelativeTime value={flow.updated_at} />
                  </p>
                </div>
              </div>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NewFlow({ orgId, onCreated }: { orgId: string; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/v1/orgs/${orgId}/flows`, {
        name: name.trim(),
        description: description.trim() || null,
      });
      onCreated();
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
