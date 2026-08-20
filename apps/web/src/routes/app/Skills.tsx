/**
 * The skill library — procedures an agent looks up instead of being told.
 *
 * The screen is built around one fact that is easy to miss: **the description
 * is the only part a model reads before choosing**. It decides whether the
 * skill is ever opened, so it is given the same prominence as the name and is
 * labelled by the question it has to answer ("when should an agent reach for
 * this?"), not by its field name.
 *
 * `SKILL.md` import is a first-class button rather than a hidden import menu,
 * because the format is Anthropic's and people arrive with folders of them
 * already written. Paste the file, get a skill.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { ApiError, api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Card, Field } from "../../components/ui";
import { PageHeader, RelativeTime } from "./bits";

export interface SkillSummary {
  id: string;
  name: string;
  description: string;
  load_count: number;
  updated_at: string;
  resource_count: number;
  instruction_chars: number;
}

interface SkillFull extends SkillSummary {
  instructions: string;
  resources: { name: string; content: string }[];
}

const INPUT =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 outline-none focus:border-brand-400";

export function Skills() {
  const { orgId } = useWorkspace();
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"none" | "write" | "import">("none");
  const [editing, setEditing] = useState<SkillFull | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setSkills(await api.get<SkillSummary[]>(`/api/v1/orgs/${orgId}/skills`));
      setError(null);
    } catch {
      setError("Could not load the skill library.");
      setSkills([]);
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function edit(skill: SkillSummary) {
    try {
      setEditing(
        await api.get<SkillFull>(`/api/v1/orgs/${orgId}/skills/${skill.id}`),
      );
      setMode("write");
    } catch {
      setError("Could not open that skill.");
    }
  }

  async function remove(skill: SkillSummary) {
    if (
      !confirm(
        `Delete "${skill.name}"? Agents that list it keep running — they just stop being offered it, ` +
          `and the run log says so.`,
      )
    )
      return;
    try {
      await api.del(`/api/v1/orgs/${orgId}/skills/${skill.id}`);
      await load();
    } catch {
      setError("Could not delete that skill.");
    }
  }

  if (skills === null) return null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Agents"
        title="Skills"
        subtitle="Procedures your agents look up when they apply. An agent is told each skill's name and description; it reads the instructions only when it needs them."
        action={
          <div className="flex gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                setEditing(null);
                setMode(mode === "import" ? "none" : "import");
              }}
            >
              Import SKILL.md
            </Button>
            <Button
              onClick={() => {
                setEditing(null);
                setMode(mode === "write" ? "none" : "write");
              }}
            >
              {mode === "write" ? "Cancel" : "New skill"}
            </Button>
          </div>
        }
      />

      {error && <Alert>{error}</Alert>}

      {mode === "import" && (
        <ImportSkill
          orgId={orgId!}
          onDone={() => {
            setMode("none");
            void load();
          }}
        />
      )}

      {mode === "write" && (
        <WriteSkill
          orgId={orgId!}
          existing={editing}
          onDone={() => {
            setMode("none");
            setEditing(null);
            void load();
          }}
        />
      )}

      {skills.length === 0 && mode === "none" ? (
        <Card className="p-10 text-center">
          <p className="text-ink-200">No skills yet.</p>
          <p className="mx-auto mt-2 max-w-lg text-sm leading-relaxed text-ink-500">
            A skill is what you would otherwise paste into every agent's prompt
            — a refund policy, a code review checklist, the way your team writes
            release notes. Write it once here, list it on an Agent node, and the
            agent opens it when the request calls for it.
          </p>
        </Card>
      ) : (
        <ul className="space-y-2.5">
          {skills.map((skill, index) => (
            <motion.li
              key={skill.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                delay: Math.min(index * 0.03, 0.25),
                duration: 0.25,
              }}
            >
              <div className="surface flex flex-wrap items-start gap-4 rounded-2xl p-5">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2.5">
                    <p className="truncate font-mono text-sm font-medium text-ink-100">
                      {skill.name}
                    </p>
                    <span className="rounded-md border border-ink-700 px-1.5 py-0.5 text-[0.68rem] text-ink-400">
                      {words(skill.instruction_chars)}
                    </span>
                    {skill.resource_count > 0 && (
                      <span className="rounded-md border border-ink-700 px-1.5 py-0.5 text-[0.68rem] text-ink-400">
                        {skill.resource_count} file
                        {skill.resource_count > 1 ? "s" : ""}
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 text-sm leading-relaxed text-ink-400">
                    {skill.description}
                  </p>
                </div>
                <div className="flex-none text-right text-xs text-ink-500">
                  <p>
                    {/* Usage, because a library nobody reads is worth knowing about. */}
                    {skill.load_count === 0
                      ? "Never loaded"
                      : `Loaded ${skill.load_count} time${skill.load_count > 1 ? "s" : ""}`}
                  </p>
                  <p className="mt-1">
                    Edited <RelativeTime value={skill.updated_at} />
                  </p>
                </div>
                <div className="flex flex-none gap-1.5">
                  <Button
                    variant="ghost"
                    onClick={() => void download(orgId!, skill)}
                    title="Download as SKILL.md"
                  >
                    Export
                  </Button>
                  <Button variant="ghost" onClick={() => void edit(skill)}>
                    Edit
                  </Button>
                  <Button variant="ghost" onClick={() => void remove(skill)}>
                    Delete
                  </Button>
                </div>
              </div>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Download a skill as a file.
 *
 * Fetched through the api client rather than linked directly, so the export
 * carries the same session handling as every other request — a plain
 * cross-origin link would be the one call that breaks the moment the cookie
 * policy tightens.
 */
async function download(orgId: string, skill: SkillSummary) {
  const body = await api.text(
    `/api/v1/orgs/${orgId}/skills/${skill.id}/export`,
  );
  const url = URL.createObjectURL(new Blob([body], { type: "text/markdown" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${skill.name}.SKILL.md`;
  link.click();
  URL.revokeObjectURL(url);
}

function words(chars: number): string {
  const count = Math.max(1, Math.round(chars / 5.5));
  return count >= 1000
    ? `${(count / 1000).toFixed(1)}k words`
    : `${count} words`;
}

function WriteSkill({
  orgId,
  existing,
  onDone,
}: {
  orgId: string;
  existing: SkillFull | null;
  onDone: () => void;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [instructions, setInstructions] = useState(
    existing?.instructions ?? "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      description: description.trim(),
      instructions,
      resources: existing?.resources ?? [],
    };
    try {
      if (existing)
        await api.put(`/api/v1/orgs/${orgId}/skills/${existing.id}`, body);
      else await api.post(`/api/v1/orgs/${orgId}/skills`, body);
      onDone();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save this skill.",
      );
      setBusy(false);
    }
  }

  return (
    <Card className="p-6">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <div className="grid gap-4 md:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
          <Field
            label="Name"
            name="name"
            required
            autoFocus
            placeholder="refund-policy"
            hint="Lowercase and hyphens — the agent passes this to a tool."
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <div>
            <label className="mb-1.5 block text-sm font-medium text-ink-300">
              When should an agent reach for this?
            </label>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Use when a customer asks for money back, including partial refunds and chargebacks."
              className={INPUT}
              required
            />
            <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
              The only part the agent reads before choosing. Describe the
              situation, not the document — “refund policy” tells it far less
              than “use when a customer asks for money back”.
            </p>
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-sm font-medium text-ink-300">
            Instructions
          </label>
          <textarea
            value={instructions}
            onChange={(event) => setInstructions(event.target.value)}
            rows={14}
            required
            placeholder={
              "# Refunds\n\n1. Check the order date.\n2. Under 30 days: refund in full."
            }
            className={`${INPUT} resize-y font-mono text-[0.78rem] leading-relaxed`}
          />
          <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
            Markdown. This is loaded whole when the agent opens the skill, so
            keep it a procedure — a long reference belongs in a bundled file.
          </p>
        </div>

        <div className="flex gap-2">
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : existing ? "Save changes" : "Create skill"}
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

function ImportSkill({ orgId, onDone }: { orgId: string; onDone: () => void }) {
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/v1/orgs/${orgId}/skills/import`, { content });
      onDone();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not import that file.",
      );
      setBusy(false);
    }
  }

  return (
    <Card className="p-6">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <div>
          <label className="mb-1.5 block text-sm font-medium text-ink-300">
            SKILL.md
          </label>
          <textarea
            value={content}
            onChange={(event) => setContent(event.target.value)}
            rows={14}
            required
            autoFocus
            placeholder={
              "---\nname: pdf-forms\ndescription: Use when the user needs to fill in a PDF form.\n---\n\n# PDF forms\n\n…"
            }
            className={`${INPUT} resize-y font-mono text-[0.78rem] leading-relaxed`}
          />
          <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
            Paste a skill file — the same format Claude uses, frontmatter and
            all. Name and description come from the frontmatter; everything
            after it becomes the instructions.
          </p>
        </div>
        <div className="flex gap-2">
          <Button type="submit" disabled={busy}>
            {busy ? "Importing…" : "Import"}
          </Button>
          <Button variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
