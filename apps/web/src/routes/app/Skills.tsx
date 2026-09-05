/**
 * The skill library: procedures an agent looks up instead of being told.
 *
 * The screen is built around one fact that is easy to miss: the description
 * is the only part a model reads before choosing. It decides whether the skill
 * is ever opened, so it is given the same prominence as the name and is
 * labelled by the question it has to answer ("when should an agent reach for
 * this?"), not by its field name.
 *
 * SKILL.md import is a first-class button rather than a hidden menu, because
 * the format is Anthropic's and people arrive with folders of them already
 * written. Paste the file, get a skill.
 */

import { motion } from "motion/react";
import {
  useCallback,
  useEffect,
  useId,
  useState,
  type FormEvent,
} from "react";

import { ApiError, api } from "../../lib/api";
import { cx } from "../../lib/cx";
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

type Dialog =
  | { kind: "create" | "import" }
  | { kind: "edit"; skill: SkillFull }
  | { kind: "delete"; skill: SkillSummary };

/** `Field`'s input styling, for the multi-line controls it does not cover. */
const TEXTAREA =
  "block w-full resize-none rounded-xl border border-ink-600/70 bg-ink-900/60 px-3.5 py-3 " +
  "font-mono text-sm leading-relaxed text-ink-100 placeholder:text-ink-500 transition-all duration-150 " +
  "hover:border-ink-500 focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none";

const ICON_BUTTON =
  "grid h-10 w-10 place-items-center rounded-xl text-ink-400 transition-colors hover:bg-ink-800 hover:text-ink-100 " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";

export function Skills() {
  const { orgId } = useWorkspace();
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

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

  const closeDialog = useCallback(() => setDialog(null), []);

  function saved() {
    setDialog(null);
    void load();
  }

  if (!orgId) return null;

  const edit = async (skill: SkillSummary) => {
    try {
      const full = await api.get<SkillFull>(
        `/api/v1/orgs/${orgId}/skills/${skill.id}`,
      );
      setDialog({ kind: "edit", skill: full });
    } catch {
      setError("Could not open that skill.");
    }
  };

  const exportSkill = async (skill: SkillSummary) => {
    try {
      await download(orgId, skill);
    } catch {
      setError("Could not export that skill.");
    }
  };

  const newButton = (
    <Button onClick={() => setDialog({ kind: "create" })}>New skill</Button>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Build"
        title="Skills"
        subtitle="A skill is a procedure an agent opens when it applies, instead of a longer prompt on every run."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              onClick={() => setDialog({ kind: "import" })}
            >
              Import SKILL.md
            </Button>
            {newButton}
          </div>
        }
      />

      {error && <Alert>{error}</Alert>}

      {skills === null ? (
        <ul
          className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
          aria-hidden="true"
        >
          {[0, 1, 2].map((i) => (
            <li key={i} className="surface h-48 animate-pulse rounded-2xl" />
          ))}
        </ul>
      ) : skills.length === 0 ? (
        <EmptyState icon={<BookIcon />} title="No skills yet" action={newButton}>
          A skill is what you would otherwise paste into every agent prompt: a
          refund policy, a review checklist, the way your team writes release
          notes. Write it once, list it on an Agent node, and the agent opens it
          when the request calls for it.
        </EmptyState>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {skills.map((skill, index) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              index={index}
              onEdit={() => void edit(skill)}
              onExport={() => void exportSkill(skill)}
              onDelete={() => setDialog({ kind: "delete", skill })}
            />
          ))}
        </ul>
      )}

      {dialog?.kind === "create" && (
        <SkillDialog orgId={orgId} onClose={closeDialog} onSaved={saved} />
      )}
      {dialog?.kind === "edit" && (
        <SkillDialog
          orgId={orgId}
          skill={dialog.skill}
          onClose={closeDialog}
          onSaved={saved}
        />
      )}
      {dialog?.kind === "import" && (
        <ImportDialog orgId={orgId} onClose={closeDialog} onSaved={saved} />
      )}
      {dialog?.kind === "delete" && (
        <DeleteDialog
          orgId={orgId}
          skill={dialog.skill}
          onClose={closeDialog}
          onDeleted={saved}
        />
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- card --- */

function SkillCard({
  skill,
  index,
  onEdit,
  onExport,
  onDelete,
}: {
  skill: SkillSummary;
  index: number;
  onEdit: () => void;
  onExport: () => void;
  onDelete: () => void;
}) {
  const files =
    skill.resource_count > 0
      ? `${skill.resource_count} file${skill.resource_count === 1 ? "" : "s"}`
      : null;

  return (
    <motion.li
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.03, 0.25), duration: 0.25 }}
      className="surface flex flex-col gap-4 rounded-2xl p-5 transition-colors hover:border-[var(--edge-strong)]"
    >
      <div className="flex items-start gap-3.5">
        <IconChip>
          <BookIcon />
        </IconChip>
        <div className="min-w-0 flex-1">
          <p
            className="truncate font-mono text-sm font-medium text-ink-100"
            title={skill.name}
          >
            {skill.name}
          </p>
          <p
            className="mt-1 line-clamp-2 text-sm leading-relaxed text-ink-300"
            title={skill.description}
          >
            {skill.description}
          </p>
        </div>
      </div>

      <p className="text-xs text-ink-400">
        {words(skill.instruction_chars)}
        {files && <> · {files}</>}
      </p>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-[var(--edge)] pt-4">
        {/* Usage, because a library nobody reads is worth knowing about. */}
        <div className="min-w-0 flex-1 text-xs leading-5 text-ink-400">
          {skill.load_count === 0 ? (
            <p>
              <Pill tone="warn">Never loaded</Pill>
            </p>
          ) : (
            <p>
              Loaded {skill.load_count} time{skill.load_count === 1 ? "" : "s"}
            </p>
          )}
          <p>
            Edited <RelativeTime value={skill.updated_at} />
          </p>
        </div>
        <div className="flex flex-none items-center gap-1">
          <Button
            variant="secondary"
            onClick={onEdit}
            aria-label={`Edit ${skill.name}`}
          >
            Edit
          </Button>
          <button
            type="button"
            onClick={onExport}
            aria-label={`Download ${skill.name} as SKILL.md`}
            title="Download as SKILL.md"
            className={ICON_BUTTON}
          >
            <DownloadIcon />
          </button>
          <button
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${skill.name}`}
            title="Delete"
            className={cx(
              ICON_BUTTON,
              "hover:bg-[color-mix(in_oklab,var(--status-bad)_12%,transparent)] hover:text-[var(--status-bad)]",
            )}
          >
            <TrashIcon />
          </button>
        </div>
      </div>
    </motion.li>
  );
}

/**
 * Download a skill as a file.
 *
 * Fetched through the api client rather than linked directly, so the export
 * carries the same session handling as every other request. A plain
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

/* ------------------------------------------------------- create / edit --- */

/** One form for both jobs. Without a `skill` it creates; with one it edits. */
function SkillDialog({
  orgId,
  skill,
  onClose,
  onSaved,
}: {
  orgId: string;
  skill?: SkillFull;
  onClose: () => void;
  onSaved: () => void;
}) {
  const formId = useId();
  const bodyId = useId();
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [instructions, setInstructions] = useState(skill?.instructions ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSave =
    name.trim().length > 0 &&
    description.trim().length > 0 &&
    instructions.trim().length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      description: description.trim(),
      instructions,
      resources: skill?.resources ?? [],
    };
    try {
      if (skill) await api.put(`/api/v1/orgs/${orgId}/skills/${skill.id}`, body);
      else await api.post(`/api/v1/orgs/${orgId}/skills`, body);
      onSaved();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save this skill.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      size="lg"
      title={skill ? "Edit skill" : "New skill"}
      description={
        skill ? (
          <>
            <span className="font-mono text-ink-300">{skill.name}</span> ·
            edited <RelativeTime value={skill.updated_at} />
          </>
        ) : (
          "Agents see the name and description on every run. They open the instructions only when the request calls for them."
        )
      }
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            loading={busy}
            disabled={!canSave}
          >
            {skill ? "Save changes" : "Create skill"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-5" noValidate>
        {error && <Alert>{error}</Alert>}
        <div className="grid gap-4 md:grid-cols-[minmax(0,240px)_minmax(0,1fr)]">
          <Field
            label="Name"
            name="name"
            required
            autoFocus={!skill}
            placeholder="refund-policy"
            hint="Lowercase and hyphens. The agent passes this to a tool."
            className="font-mono"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <Field
            label="When should an agent reach for this?"
            name="description"
            required
            placeholder="Use when a customer asks for money back, including partial refunds and chargebacks."
            hint="The only part the agent reads before choosing. Describe the situation, not the document."
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>

        <div className="space-y-1.5">
          <label
            htmlFor={bodyId}
            className="block text-sm font-medium text-ink-200"
          >
            Instructions
          </label>
          <textarea
            id={bodyId}
            value={instructions}
            onChange={(event) => setInstructions(event.target.value)}
            required
            spellCheck={false}
            placeholder={
              "# Refunds\n\n1. Check the order date.\n2. Under 30 days: refund in full."
            }
            className={`${TEXTAREA} min-h-[40vh]`}
          />
          <p className="text-sm text-ink-400">
            Markdown. Loaded whole when the agent opens the skill, so keep it a
            procedure. A long reference belongs in a bundled file.
          </p>
        </div>

        {skill && skill.resources.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-sm font-medium text-ink-200">Bundled files</p>
            <ul className="flex flex-wrap gap-1.5">
              {skill.resources.map((resource) => (
                <li key={resource.name}>
                  <Pill>
                    <span className="font-mono">{resource.name}</span>
                  </Pill>
                </li>
              ))}
            </ul>
            <p className="text-sm text-ink-400">Kept as they are when you save.</p>
          </div>
        )}
      </form>
    </Modal>
  );
}

/* -------------------------------------------------------------- import --- */

function ImportDialog({
  orgId,
  onClose,
  onSaved,
}: {
  orgId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const formId = useId();
  const fileId = useId();
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!content.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/v1/orgs/${orgId}/skills/import`, { content });
      onSaved();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not import that file.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      size="lg"
      title="Import SKILL.md"
      description="Paste a skill file, the same format Claude uses, frontmatter and all. The name and description come from the frontmatter. Everything after it becomes the instructions."
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            loading={busy}
            disabled={!content.trim()}
          >
            Import
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <div className="space-y-1.5">
          <label
            htmlFor={fileId}
            className="block text-sm font-medium text-ink-200"
          >
            File contents
          </label>
          <textarea
            id={fileId}
            value={content}
            onChange={(event) => setContent(event.target.value)}
            required
            autoFocus
            spellCheck={false}
            placeholder={
              "---\nname: pdf-forms\ndescription: Use when the user needs to fill in a PDF form.\n---\n\n# PDF forms\n\n…"
            }
            className={`${TEXTAREA} min-h-[48vh]`}
          />
        </div>
      </form>
    </Modal>
  );
}

/* -------------------------------------------------------------- delete --- */

function DeleteDialog({
  orgId,
  skill,
  onClose,
  onDeleted,
}: {
  orgId: string;
  skill: SkillSummary;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.del(`/api/v1/orgs/${orgId}/skills/${skill.id}`);
      onDeleted();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete that skill.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      size="sm"
      title="Delete skill"
      description="This cannot be undone."
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          {/* Solid status-bad with ink-950 text: the positional scale flips
              with the theme, so the text stays legible on both the pale
              dark-theme rose and the deep light-theme one. */}
          <Button
            onClick={() => void remove()}
            loading={busy}
            className="hover:brightness-110"
            style={{
              background: "var(--status-bad)",
              color: "var(--color-ink-950)",
            }}
          >
            Delete skill
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <div className="flex items-center gap-3 rounded-xl border border-[var(--edge)] bg-ink-900/50 px-4 py-3">
          <IconChip size="sm">
            <BookIcon />
          </IconChip>
          <div className="min-w-0">
            <p className="truncate font-mono text-sm font-medium text-ink-100">
              {skill.name}
            </p>
            <p className="truncate text-xs text-ink-400" title={skill.description}>
              {skill.description}
            </p>
          </div>
        </div>
        <p className="text-sm leading-relaxed text-ink-300">
          Agents that list this skill keep running. They stop being offered it,
          and the run log says so.
        </p>
      </div>
    </Modal>
  );
}

/* --------------------------------------------------------------- icons --- */

function BookIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 7.2C10.3 5.9 8 5.4 4 5.6v12.6c4-.2 6.3.3 8 1.6 1.7-1.3 4-1.8 8-1.6V5.6c-4-.2-6.3.3-8 1.6Z" />
      <path d="M12 7.2v12.6" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 4v10.5M7.5 10.5 12 15l4.5-4.5M5 17.5v1a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5v-1" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2" />
    </svg>
  );
}
