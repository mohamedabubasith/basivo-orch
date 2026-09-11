/**
 * Apps: the list of things somebody has described into existence.
 *
 * A card per project, and one button. Creating an app asks for a name and
 * nothing else, because the whole promise is that the next thing you do is
 * describe what you want rather than choose a framework, a template and a
 * package manager. The agent, the credential and the rest are decisions the
 * builder makes on your behalf and are changeable later.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api, isSessionEnded } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import {
  Alert,
  Button,
  EmptyState,
  Field,
  IconChip,
  Modal,
  Pill,
} from "../../components/ui";
import { PageHeader, RelativeTime } from "./bits";

export interface AppProject {
  id: string;
  name: string;
  slug: string;
  engine: string;
  created_at: string;
  updated_at: string;
  latest_version: number | null;
  published_version: number | null;
  preview_url: string;
  share_url: string;
  busy: boolean;
}

export default function Apps() {
  const { orgId } = useWorkspace();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<AppProject[] | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setProjects(await api.get<AppProject[]>(`/api/v1/orgs/${orgId}/apps`));
      setError("");
    } catch (err) {
      if (isSessionEnded(err)) return;
      setError("Could not load your apps.");
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!orgId || !name.trim()) return;
    setBusy(true);
    try {
      const project = await api.post<AppProject>(`/api/v1/orgs/${orgId}/apps`, {
        name: name.trim(),
      });
      setCreating(false);
      setName("");
      navigate(`/app/apps/${project.id}`);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "That app could not be created.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!orgId) return null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Build"
        title="Apps"
        subtitle="Describe a page and watch it appear. Deploy the version you like and send the link to anyone."
        action={<Button onClick={() => setCreating(true)}>New app</Button>}
      />

      {error && <Alert tone="error">{error}</Alert>}

      {projects && projects.length === 0 && (
        <EmptyState
          title="No apps yet"
          action={<Button onClick={() => setCreating(true)}>New app</Button>}
        >
          Start one and tell it what you want: a landing page, a menu, a form,
          an internal dashboard.
        </EmptyState>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {(projects ?? []).map((project, index) => (
          <motion.button
            key={project.id}
            type="button"
            onClick={() => navigate(`/app/apps/${project.id}`)}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: Math.min(index, 8) * 0.03 }}
            className="rounded-2xl border border-ink-700/70 bg-ink-900/50 p-5 text-left transition hover:border-ink-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-base font-medium text-ink-50">
                  {project.name}
                </h2>
                <p className="mt-1 text-sm text-ink-400">
                  edited <RelativeTime value={project.updated_at} />
                </p>
              </div>
              <IconChip>
                <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
                  <path
                    fill="currentColor"
                    d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Zm2 .5v2h12V6H6Zm12 4H6v8h12v-8Z"
                  />
                </svg>
              </IconChip>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              {project.busy ? (
                <Pill tone="info">working</Pill>
              ) : project.published_version ? (
                <Pill tone="good">deployed v{project.published_version}</Pill>
              ) : project.latest_version ? (
                <Pill>built, not deployed</Pill>
              ) : (
                <Pill>empty</Pill>
              )}
            </div>
          </motion.button>
        ))}
      </div>

      {creating && (
        <Modal
          onClose={() => setCreating(false)}
          title="New app"
          description="Give it a name. You describe what it does in the next screen."
        >
          <form onSubmit={create} className="space-y-4">
            <Field
              label="Name"
              value={name}
              autoFocus
              maxLength={160}
              onChange={(event) => setName(event.target.value)}
              placeholder="Bakery landing page"
            />
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setCreating(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={busy || !name.trim()}>
                {busy ? "Creating" : "Create"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
