/**
 * The two dropdowns that make a credential and a model pickable rather than
 * typed.
 *
 * Extracted from the Inspector when sub-agents needed them too: a second copy
 * would drift within a week, and a UUID typed by hand is not a feature.
 */

import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { cx } from "../lib/cx";

const INPUT =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-2.5 py-2 text-sm text-ink-100 outline-none focus:border-brand-400";

interface CredentialOption {
  id: string;
  name: string;
  provider: string;
  hint?: string;
}

/**
 * The Agent node's credential field.
 *
 * A plain text input here would ask someone to paste an id they have never
 * seen — `credential_id` is a UUID, not something a person picks out of thin
 * air. This fetches the workspace's saved credentials, filters to ones that
 * match the selected provider (the API rejects a mismatch at run time — see
 * `AgentNode._build_model` — so surfacing the same rule here keeps a person
 * from picking a combination that is going to fail), and falls back to naming
 * a real path when none exist: use the server's own environment-variable key,
 * or go create one.
 */
export function CredentialPicker({
  orgId,
  provider,
  value,
  onChange,
}: {
  orgId?: string | null;
  provider: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [credentials, setCredentials] = useState<CredentialOption[] | null>(
    null,
  );

  useEffect(() => {
    if (!orgId) return;
    let cancelled = false;
    api
      .get<CredentialOption[]>(`/api/v1/orgs/${orgId}/credentials`)
      .then((list) => !cancelled && setCredentials(list))
      .catch(() => !cancelled && setCredentials([]));
    return () => {
      cancelled = true;
    };
  }, [orgId]);

  const matching = (credentials ?? []).filter((c) => c.provider === provider);

  return (
    <div>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={INPUT}
      >
        <option value="">
          {provider === "github" || provider === "gitlab"
            ? "Pick a credential…"
            : "Use the server's own key (no credential)"}
        </option>
        {matching.map((credential) => (
          <option key={credential.id} value={credential.id}>
            {credential.name} (…{credential.hint || "????"})
          </option>
        ))}
      </select>
      {credentials !== null && matching.length === 0 && (
        <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
          No saved credential for this provider yet.{" "}
          <a
            href="/app/credentials"
            target="_blank"
            rel="noreferrer"
            className="text-ink-300 underline decoration-dotted underline-offset-2 hover:text-ink-100"
          >
            Add one
          </a>
          , or leave this on the server's key if one is configured.
        </p>
      )}
    </div>
  );
}

/**
 * The Agent node's model field: a live dropdown when it can be, free text
 * when it cannot.
 *
 * The list comes from the selected credential's own provider account —
 * `GET /credentials/{id}/models` — so it shows the models *that key* can
 * actually call, not a hardcoded list that goes stale the week a provider
 * ships something new. With no credential selected, or a provider whose
 * catalog cannot be fetched (`supported: false`), it stays a plain text
 * input; every provider accepts a typed model id regardless.
 */
export function ModelPicker({
  orgId,
  credentialId,
  value,
  onChange,
}: {
  orgId?: string | null;
  credentialId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [models, setModels] = useState<string[] | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setModels(null);
    setFetchError(null);
    if (!orgId || !credentialId) return;
    let cancelled = false;
    setLoading(true);
    api
      .get<{ supported: boolean; models: string[]; error: string | null }>(
        `/api/v1/orgs/${orgId}/credentials/${credentialId}/models`,
      )
      .then((result) => {
        if (cancelled) return;
        if (result.supported && !result.error) setModels(result.models);
        else if (result.error) setFetchError(result.error);
      })
      .catch(
        () => !cancelled && setFetchError("Could not fetch the model list."),
      )
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [orgId, credentialId]);

  if (models && models.length > 0) {
    return (
      <div>
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={INPUT}
        >
          {/* A saved model that the key can no longer see (renamed, retired,
              or configured before the credential) must not be silently
              swapped for the first list entry — keep it selectable and let
              the run surface the provider's own error if it is truly gone. */}
          {value && !models.includes(value) && (
            <option value={value}>{value} (saved)</option>
          )}
          {models.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
        <p className="mt-1 text-[0.68rem] leading-relaxed text-ink-500">
          {models.length} models available to this credential.
        </p>
      </div>
    );
  }

  return (
    <div>
      <input
        value={value}
        placeholder="e.g. claude-sonnet-5"
        onChange={(event) => onChange(event.target.value)}
        className={cx(INPUT, "font-mono text-[0.8rem]")}
      />
      {loading && (
        <p className="mt-1 text-[0.68rem] text-ink-500">Fetching model list…</p>
      )}
      {fetchError && (
        <p
          className="mt-1 text-[0.68rem]"
          style={{ color: "var(--status-warn)" }}
        >
          Model list unavailable: {fetchError.slice(0, 120)}
        </p>
      )}
      {!loading && !fetchError && !credentialId && (
        <p className="mt-1 text-[0.68rem] text-ink-500">
          Pick a credential above to load its live model list.
        </p>
      )}
    </div>
  );
}

/**
 * The code node's editor: a real writing surface, not a one-line input.
 *
 * Not a full CodeMirror — that is a heavyweight dependency for a beta whose
 * code blocks are a screenful — but the things that make a textarea unusable
 * for code are fixed: monospace, tall, no spellcheck/autocorrect mangling,
 * and Tab inserts indentation instead of throwing focus to the next field,
 * which is the single behaviour that makes people give up on textarea code.
 */
interface SkillOption {
  id: string;
  name: string;
  description: string;
  instruction_chars: number;
  resource_count: number;
}

/**
 * Which of the workspace's skills this agent may use.
 *
 * Checkboxes rather than a multi-select, because the description has to be
 * visible while choosing: it is what the agent reads when deciding whether to
 * open the skill, so a picker that shows only names hides the thing that
 * actually determines behaviour.
 *
 * The count of selected skills is shown as prompt weight, not as a number of
 * items — ten skills cost ten lines, and saying so is what stops someone
 * ticking every box "just in case" and wondering later why runs got slower.
 */
export function SkillPicker({
  orgId,
  value,
  onChange,
}: {
  orgId?: string | null;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const [skills, setSkills] = useState<SkillOption[] | null>(null);

  useEffect(() => {
    if (!orgId) return;
    let cancelled = false;
    api
      .get<SkillOption[]>(`/api/v1/orgs/${orgId}/skills`)
      .then((list) => !cancelled && setSkills(list))
      .catch(() => !cancelled && setSkills([]));
    return () => {
      cancelled = true;
    };
  }, [orgId]);

  if (skills === null)
    return <p className="text-[0.7rem] text-ink-500">Loading skills…</p>;

  if (skills.length === 0)
    return (
      <p className="text-[0.7rem] leading-relaxed text-ink-500">
        No skills in this workspace yet. Write one under{" "}
        <a
          href="/app/skills"
          className="text-brand-300 underline underline-offset-2"
        >
          Skills
        </a>{" "}
        — a procedure the agent opens when it applies, instead of a longer
        prompt on every run.
      </p>
    );

  const selected = new Set(value);
  // What ticking a box actually costs: one catalogue line each, now; the body
  // only if the agent opens it.
  const lines = selected.size;

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    // Kept in library order rather than click order, so two agents with the
    // same skills produce the same graph JSON.
    onChange(
      skills!.filter((skill) => next.has(skill.id)).map((skill) => skill.id),
    );
  }

  return (
    <div>
      <ul className="max-h-64 space-y-1 overflow-y-auto rounded-lg border border-ink-700 bg-ink-950/40 p-1.5">
        {skills.map((skill) => {
          const on = selected.has(skill.id);
          return (
            <li key={skill.id}>
              <label
                className={cx(
                  "flex cursor-pointer gap-2.5 rounded-lg p-2 transition-colors",
                  on ? "bg-ink-800/70" : "hover:bg-ink-800/40",
                )}
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => toggle(skill.id)}
                  className="mt-0.5 h-3.5 w-3.5 flex-none accent-[var(--color-brand-400)]"
                />
                <span className="min-w-0">
                  <span className="block font-mono text-[0.72rem] text-ink-100">
                    {skill.name}
                  </span>
                  <span className="mt-0.5 block text-[0.68rem] leading-relaxed text-ink-500">
                    {skill.description}
                  </span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
      <p className="mt-1.5 text-[0.68rem] leading-relaxed text-ink-500">
        {lines === 0
          ? "None selected — the agent is not told the library exists."
          : `${lines} skill${lines > 1 ? "s" : ""} offered: ${lines} line${
              lines > 1 ? "s" : ""
            } of prompt now, and the full instructions only if the agent opens one.`}
      </p>
    </div>
  );
}
