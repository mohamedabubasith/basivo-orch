/**
 * Provider credentials — created once, picked by name inside an Agent node.
 *
 * The secret is entered exactly once and never appears again: the API's
 * `CredentialRead` schema has no field for it, so there is no response this
 * page could show it in even by accident. What is shown instead is a `hint` —
 * the key's last four characters — enough to tell two credentials apart
 * without exposing either. Editing keeps the rule: a new key can be pasted
 * over the old one, the old one is never read back.
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
import { PROVIDER_LABEL, PROVIDERS } from "../../builder/providers";

export interface Credential {
  id: string;
  name: string;
  provider: string;
  hint: string;
  base_url: string | null;
  created_at: string;
  last_used_at: string | null;
}

type Dialog =
  | { kind: "create" }
  | { kind: "edit" | "delete"; credential: Credential };

/** One hue per provider family, so a card reads as "a GitHub thing" before
 *  its text does — the same colours the rest of the app uses for them. */
function providerHue(provider: string): string {
  switch (provider) {
    case "github":
    case "gitlab":
      return "#94a3b8";
    case "jira":
      return "#2684ff";
    case "mcp":
      return "var(--color-accent-500)";
    default:
      return "var(--color-brand-400)";
  }
}

function providerLabel(provider: string): string {
  return PROVIDER_LABEL[provider] ?? provider;
}

export function Credentials() {
  const { orgId } = useWorkspace();
  const [credentials, setCredentials] = useState<Credential[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setCredentials(
        await api.get<Credential[]>(`/api/v1/orgs/${orgId}/credentials`),
      );
      setError(null);
    } catch {
      setError("Could not load credentials.");
      setCredentials([]);
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

  const newButton = (
    <Button onClick={() => setDialog({ kind: "create" })}>
      New credential
    </Button>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Account"
        title="Credentials"
        subtitle="Keys your agent nodes use by name. Stored encrypted, never embedded in a flow."
        action={newButton}
      />

      {error && <Alert>{error}</Alert>}

      {credentials === null ? (
        <ul
          className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
          aria-hidden="true"
        >
          {[0, 1, 2].map((i) => (
            <li key={i} className="surface h-44 animate-pulse rounded-2xl" />
          ))}
        </ul>
      ) : credentials.length === 0 ? (
        <EmptyState
          icon={<KeyIcon />}
          title="No credentials yet"
          action={newButton}
        >
          Add a key for Anthropic, OpenAI, Google, Groq, Mistral or a dozen
          more model providers, or for GitHub, GitLab, Jira and MCP servers.
          Every Agent node in this workspace can pick it by name.
        </EmptyState>
      ) : (
        <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {credentials.map((credential, index) => (
            <CredentialCard
              key={credential.id}
              credential={credential}
              index={index}
              onEdit={() => setDialog({ kind: "edit", credential })}
              onDelete={() => setDialog({ kind: "delete", credential })}
            />
          ))}
        </ul>
      )}

      {dialog?.kind === "create" && (
        <CredentialDialog orgId={orgId} onClose={closeDialog} onSaved={saved} />
      )}
      {dialog?.kind === "edit" && (
        <CredentialDialog
          orgId={orgId}
          credential={dialog.credential}
          onClose={closeDialog}
          onSaved={saved}
        />
      )}
      {dialog?.kind === "delete" && (
        <DeleteDialog
          orgId={orgId}
          credential={dialog.credential}
          onClose={closeDialog}
          onDeleted={saved}
        />
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- card --- */

function CredentialCard({
  credential,
  index,
  onEdit,
  onDelete,
}: {
  credential: Credential;
  index: number;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const label = providerLabel(credential.provider);

  return (
    <motion.li
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.03, 0.25), duration: 0.25 }}
      className="surface flex flex-col gap-4 rounded-2xl p-5 transition-colors hover:border-[var(--edge-strong)]"
    >
      <div className="flex items-start gap-3.5">
        <IconChip hue={providerHue(credential.provider)}>
          {label.charAt(0).toUpperCase()}
        </IconChip>
        <div className="min-w-0 flex-1">
          <p
            className="truncate font-medium text-ink-100"
            title={credential.name}
          >
            {credential.name}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <Pill>{label}</Pill>
          </div>
        </div>
      </div>

      <div className="min-w-0 space-y-1">
        <p className="font-mono text-sm tracking-wide text-ink-200">
          {credential.hint ? `•••• •••• ${credential.hint}` : "No key preview"}
        </p>
        {credential.base_url && (
          <p
            className="truncate font-mono text-xs text-ink-400"
            title={credential.base_url}
          >
            {credential.base_url}
          </p>
        )}
      </div>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-[var(--edge)] pt-4">
        {/* Two short lines, not one long one: a 3-column card is too narrow to
            hold "Last used … · Added …" beside the buttons without wrapping
            mid-sentence. */}
        <div className="min-w-0 flex-1 text-xs leading-5 text-ink-400">
          {credential.last_used_at ? (
            <p>
              Last used <RelativeTime value={credential.last_used_at} />
            </p>
          ) : (
            <p>
              <Pill tone="warn">Never used</Pill>
            </p>
          )}
          <p>
            Added <RelativeTime value={credential.created_at} />
          </p>
        </div>
        <div className="flex flex-none items-center gap-1">
          <Button
            variant="secondary"
            onClick={onEdit}
            aria-label={`Edit ${credential.name}`}
          >
            Edit
          </Button>
          <button
            type="button"
            onClick={onDelete}
            aria-label={`Delete ${credential.name}`}
            className="grid h-10 w-10 place-items-center rounded-xl text-ink-400 transition-colors hover:bg-[color-mix(in_oklab,var(--status-bad)_12%,transparent)] hover:text-[var(--status-bad)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <TrashIcon />
          </button>
        </div>
      </div>
    </motion.li>
  );
}

/* ------------------------------------------------------- create / edit --- */

interface TestResult {
  supported: boolean;
  models: string[];
  error: string | null;
}

/**
 * One form for both jobs. Without a `credential` it creates; with one it
 * edits, and PATCHes only the fields that actually changed — an omitted
 * `api_key` keeps the stored key, which is how "leave empty" works.
 */
function CredentialDialog({
  orgId,
  credential,
  onClose,
  onSaved,
}: {
  orgId: string;
  credential?: Credential;
  onClose: () => void;
  onSaved: () => void;
}) {
  const formId = useId();
  const providerId = useId();
  const [name, setName] = useState(credential?.name ?? "");
  const [provider, setProvider] = useState(
    credential?.provider ?? PROVIDERS[0].value,
  );
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(credential?.base_url ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<TestResult | null>(null);

  const trimmedName = name.trim();
  const url = baseUrl.trim() || null;
  const hasKey = apiKey.trim().length > 0;

  const changes: { name?: string; base_url?: string | null; api_key?: string } =
    {};
  if (credential) {
    if (trimmedName !== credential.name) changes.name = trimmedName;
    if (url !== credential.base_url) changes.base_url = url;
    if (hasKey) changes.api_key = apiKey;
  }
  const canSave = credential
    ? trimmedName.length > 0 && Object.keys(changes).length > 0
    : trimmedName.length > 0 && hasKey;

  // A result describes one exact (provider, key, url) combination; keeping it
  // on screen after any of those change would show a verdict about a
  // credential that no longer exists.
  function invalidateTest() {
    setTested(null);
  }

  async function testConnection() {
    setTesting(true);
    setTested(null);
    try {
      setTested(
        await api.post<TestResult>(`/api/v1/orgs/${orgId}/credentials/test`, {
          provider,
          api_key: apiKey,
          base_url: url,
        }),
      );
    } catch (err) {
      setTested({
        supported: true,
        models: [],
        error:
          err instanceof ApiError
            ? err.message
            : "The test request did not go through.",
      });
    } finally {
      setTesting(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSave) return;
    setBusy(true);
    setError(null);
    try {
      if (credential) {
        await api.patch<Credential>(
          `/api/v1/orgs/${orgId}/credentials/${credential.id}`,
          changes,
        );
      } else {
        await api.post(`/api/v1/orgs/${orgId}/credentials`, {
          name: trimmedName,
          provider,
          api_key: apiKey,
          base_url: url,
        });
      }
      onSaved();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save this credential.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      title={credential ? "Edit credential" : "New credential"}
      description={
        credential ? (
          <>
            {providerLabel(credential.provider)} · key ending{" "}
            <span className="font-mono text-ink-300">
              {credential.hint || "unknown"}
            </span>
          </>
        ) : (
          "Stored encrypted and never shown again. Agent nodes refer to it by name."
        )
      }
      onClose={onClose}
      footer={
        <>
          {credential && !hasKey && (
            <span className="mr-auto text-xs text-ink-400">
              Type a new key to test it
            </span>
          )}
          <Button
            type="button"
            variant="secondary"
            onClick={() => void testConnection()}
            loading={testing}
            disabled={!hasKey}
          >
            Test connection
          </Button>
          <Button
            type="submit"
            form={formId}
            loading={busy}
            disabled={!canSave}
          >
            {credential ? "Save changes" : "Save credential"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <Field
          label="Name"
          name="name"
          required
          autoFocus
          placeholder="Production Anthropic key"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        {!credential && (
          <div className="space-y-1.5">
            <label
              htmlFor={providerId}
              className="block text-sm font-medium text-ink-200"
            >
              Provider
            </label>
            <div className="relative">
              <select
                id={providerId}
                value={provider}
                onChange={(event) => {
                  setProvider(event.target.value);
                  invalidateTest();
                }}
                className="w-full appearance-none rounded-xl border border-ink-600/70 bg-ink-900/70 py-2.5 pr-10 pl-3.5 text-[0.95rem] text-ink-100 transition-all duration-150 hover:border-ink-500 focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none"
              >
                {PROVIDERS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <svg
                viewBox="0 0 24 24"
                className="pointer-events-none absolute top-1/2 right-3.5 h-4 w-4 -translate-y-1/2 text-ink-400"
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
            </div>
          </div>
        )}
        <Field
          label={credential ? "Replace key" : "API key"}
          name="api_key"
          type="password"
          revealable
          required={!credential}
          autoComplete="off"
          placeholder={
            credential
              ? "Paste a new key to replace the stored one"
              : "Pasted once, never shown again"
          }
          hint={credential ? "Leave empty to keep the current key" : undefined}
          value={apiKey}
          onChange={(event) => {
            setApiKey(event.target.value);
            invalidateTest();
          }}
        />
        {provider === "anthropic" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-400">
            On a Claude Pro or Max plan with no API key? Run{" "}
            <code className="rounded-md bg-ink-800 px-1.5 py-0.5 font-mono text-xs text-ink-100">
              claude setup-token
            </code>{" "}
            on your own computer and paste the token it prints, which starts
            with sk-ant-oat01. It signs in Claude Code for Fix Code and Open PR.
            The AI Agent node still needs a real API key.
          </p>
        )}
        {provider === "mcp" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-400">
            The token an MCP server expects. An agent node that names the
            server sends it as Authorization: Bearer. There is nothing to test
            it against until a node says which server, so Test connection is
            skipped for this provider.
          </p>
        )}
        {provider === "jira" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-400">
            Write the key as{" "}
            <code className="rounded-md bg-ink-800 px-1.5 py-0.5 font-mono text-xs text-ink-100">
              you@company.com:API-token
            </code>
            , the email of a Jira administrator and a token from
            id.atlassian.com (Security, API tokens). Put the site, for example
            https://your-team.atlassian.net, in Base URL below.
          </p>
        )}
        <Field
          label="Base URL"
          name="base_url"
          placeholder={
            provider === "jira"
              ? "https://your-team.atlassian.net"
              : "Optional, for a proxy, gateway or self-hosted endpoint"
          }
          value={baseUrl}
          onChange={(event) => {
            setBaseUrl(event.target.value);
            invalidateTest();
          }}
        />
        {tested && !tested.supported && apiKey.startsWith("sk-ant-oat") && (
          <Alert tone="info">
            Subscription token recognised. It signs in Claude Code for the Fix
            Code and Open PR node on your own plan. It cannot list models or
            run the AI Agent node; those need an API key.
          </Alert>
        )}
        {tested && !tested.supported && !apiKey.startsWith("sk-ant-oat") && (
          <Alert tone="info">
            This provider has no model-list endpoint to test against. The key
            will be verified on the Agent node&rsquo;s first real call instead.
          </Alert>
        )}
        {tested?.supported && tested.error && (
          <Alert>
            The key was rejected:{" "}
            <span className="font-mono text-xs">{tested.error}</span>
          </Alert>
        )}
        {tested?.supported && !tested.error && (
          <Alert tone="success">
            Connected. {tested.models.length} model
            {tested.models.length === 1 ? "" : "s"} available
            {tested.models.length > 0 && (
              <span className="text-xs">
                {" "}
                e.g. {tested.models.slice(0, 3).join(", ")}
                {tested.models.length > 3 ? ", …" : ""}
              </span>
            )}
          </Alert>
        )}
      </form>
    </Modal>
  );
}

/* -------------------------------------------------------------- delete --- */

function DeleteDialog({
  orgId,
  credential,
  onClose,
  onDeleted,
}: {
  orgId: string;
  credential: Credential;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const label = providerLabel(credential.provider);

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.del(`/api/v1/orgs/${orgId}/credentials/${credential.id}`);
      onDeleted();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not delete that credential.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      size="sm"
      title="Delete credential"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => void remove()}
            loading={busy}
            className="hover:opacity-90"
            // Inline so it beats the primary variant's own hover colour.
            style={{ background: "var(--status-bad)" }}
          >
            Delete credential
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <div className="flex items-center gap-3 rounded-xl border border-[var(--edge)] bg-ink-900/50 px-4 py-3">
          <IconChip size="sm" hue={providerHue(credential.provider)}>
            {label.charAt(0).toUpperCase()}
          </IconChip>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink-100">
              {credential.name}
            </p>
            <p className="text-xs text-ink-400">
              {label}
              {credential.hint && (
                <>
                  {" "}
                  · <span className="font-mono">•••• {credential.hint}</span>
                </>
              )}
            </p>
          </div>
        </div>
        <p className="text-sm leading-relaxed text-ink-300">
          Any agent node using this credential will fail until you point it at
          another one. This cannot be undone.
        </p>
      </div>
    </Modal>
  );
}

/* --------------------------------------------------------------- icons --- */

function KeyIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle
        cx="8.5"
        cy="15.5"
        r="4.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        d="M11.8 12.2 20.5 3.5M16.5 7.5l2.5 2.5M14 10l2 2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden="true">
      <path
        d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
