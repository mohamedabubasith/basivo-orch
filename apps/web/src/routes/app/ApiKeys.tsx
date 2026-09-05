/**
 * API keys for the external execution API.
 *
 * The screen is arranged around the one irreversible moment: the key is shown
 * exactly once, because only its hash is stored. The create dialog turns into
 * that reveal on success and says so before Done is pressed, which is what
 * stops someone closing it and discovering the loss afterwards.
 */

import { motion } from "motion/react";
import {
  useCallback,
  useEffect,
  useId,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

import { ApiError, api } from "../../lib/api";
import { loadConfig } from "../../lib/config";
import { useWorkspace } from "../../lib/workspace";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  IconChip,
  Modal,
  PageLoader,
  Pill,
  type Tone,
} from "../../components/ui";
import { PageHeader, RelativeTime } from "./bits";

interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

type Dialog = { kind: "create" } | { kind: "revoke"; key: ApiKey };

function isExpired(key: ApiKey): boolean {
  return (
    key.expires_at !== null && new Date(key.expires_at).getTime() < Date.now()
  );
}

function keyStatus(key: ApiKey): { label: string; tone: Tone } {
  if (key.revoked_at) return { label: "Revoked", tone: "bad" };
  if (isExpired(key)) return { label: "Expired", tone: "neutral" };
  return { label: "Active", tone: "good" };
}

export function ApiKeys() {
  const { orgId } = useWorkspace();
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setKeys(await api.get<ApiKey[]>(`/api/v1/orgs/${orgId}/api-keys`));
      setError(null);
    } catch {
      setError("Could not load API keys.");
      setKeys([]);
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  const closeDialog = useCallback(() => setDialog(null), []);

  if (!orgId) return null;
  if (keys === null) return <PageLoader label="Loading API keys" />;

  const newButton = (
    <Button onClick={() => setDialog({ kind: "create" })}>New API key</Button>
  );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Account"
        title="API keys"
        subtitle="Keys authenticate calls to your published flows from code outside this console."
        action={newButton}
      />

      {error && <Alert>{error}</Alert>}

      {keys.length === 0 ? (
        <EmptyState icon={<KeyIcon />} title="No API keys yet" action={newButton}>
          Create one to run a published flow from your own code or CI. Work
          inside this console uses your session, so nothing here needs a key.
        </EmptyState>
      ) : (
        <KeyTable
          keys={keys}
          onRevoke={(key) => setDialog({ kind: "revoke", key })}
        />
      )}

      <HowToCall />

      {dialog?.kind === "create" && (
        <CreateDialog
          orgId={orgId}
          onClose={closeDialog}
          onCreated={() => void load()}
        />
      )}
      {dialog?.kind === "revoke" && (
        <RevokeDialog
          orgId={orgId}
          apiKey={dialog.key}
          onClose={closeDialog}
          onRevoked={() => {
            setDialog(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

/* --------------------------------------------------------------- table --- */

function KeyTable({
  keys,
  onRevoke,
}: {
  keys: ApiKey[];
  onRevoke: (key: ApiKey) => void;
}) {
  return (
    /* Scroll rather than clip: a table cannot shrink below its content, and a
       clipped actions column is an unreachable revoke button. */
    <Card className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-[var(--edge)] bg-ink-900/40 text-xs font-medium tracking-[0.06em] text-ink-400 uppercase">
            <th scope="col" className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
              Key
            </th>
            <th scope="col" className="hidden px-4 py-3 md:table-cell">
              Created
            </th>
            <th scope="col" className="px-3 py-3 md:px-4">
              Last used
            </th>
            <th scope="col" className="px-3 py-3 md:px-4">
              Status
            </th>
            <th scope="col" className="py-3 pr-2 pl-1 md:pr-4 md:pl-4">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key, index) => {
            const status = keyStatus(key);
            // The date that explains the status: when it was revoked, or when
            // it expires or expired. An active key with no expiry has none.
            const since = key.revoked_at ?? key.expires_at;
            return (
              <motion.tr
                key={key.id}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(index * 0.02, 0.2), duration: 0.2 }}
                className="border-b border-[var(--edge)] transition-colors last:border-b-0 hover:bg-ink-800/40"
              >
                <td className="py-3 pr-3 pl-4 md:pr-4 md:pl-5">
                  <div className="flex min-w-0 items-center gap-3.5">
                    <IconChip
                      hue={
                        status.tone === "good"
                          ? "var(--color-brand-400)"
                          : "var(--color-ink-400)"
                      }
                      className="max-sm:hidden"
                    >
                      <KeyIcon />
                    </IconChip>
                    <div className="min-w-0">
                      <p
                        className="font-medium text-ink-100 wrap-anywhere md:truncate"
                        title={key.name}
                      >
                        {key.name}
                      </p>
                      <p className="mt-0.5 font-mono text-xs text-ink-400">
                        {key.prefix}…
                      </p>
                      {/* The column hidden on small screens, folded into the
                          one that stays. */}
                      <p className="mt-1 text-xs text-ink-400 md:hidden">
                        Created <RelativeTime value={key.created_at} />
                      </p>
                    </div>
                  </div>
                </td>
                <td className="hidden px-4 py-3 whitespace-nowrap text-ink-300 md:table-cell">
                  <RelativeTime value={key.created_at} />
                </td>
                <td className="px-3 py-3 whitespace-nowrap md:px-4">
                  {key.last_used_at ? (
                    <span className="text-ink-300">
                      <RelativeTime value={key.last_used_at} />
                    </span>
                  ) : (
                    <Pill tone="neutral">Never used</Pill>
                  )}
                </td>
                <td className="px-3 py-3 md:px-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone={status.tone} dot>
                      {status.label}
                    </Pill>
                    {since && (
                      <span className="text-xs whitespace-nowrap text-ink-400">
                        {status.tone === "good" && "Expires "}
                        <RelativeTime value={since} />
                      </span>
                    )}
                  </div>
                </td>
                <td className="py-3 pr-2 pl-1 md:pr-4 md:pl-4">
                  <div className="flex justify-end">
                    {!key.revoked_at && (
                      <button
                        type="button"
                        onClick={() => onRevoke(key)}
                        aria-label={`Revoke ${key.name}`}
                        title="Revoke this key"
                        className="grid h-9 w-9 place-items-center rounded-lg text-ink-400 transition-colors hover:bg-[color-mix(in_oklab,var(--status-bad)_12%,transparent)] hover:text-[var(--status-bad)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                      >
                        <RevokeIcon />
                      </button>
                    )}
                  </div>
                </td>
              </motion.tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}

/* -------------------------------------------------------------- create --- */

function CreateDialog({
  orgId,
  onClose,
  onCreated,
}: {
  orgId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const formId = useId();
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The full key, present only between creation and Done. */
  const [issued, setIssued] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const trimmed = name.trim();

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<{ key: string }>(
        `/api/v1/orgs/${orgId}/api-keys`,
        { name: trimmed, expires_in_days: days ? Number(days) : null },
      );
      setIssued(created.key);
      onCreated();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create the key.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (issued) {
    return (
      <Modal
        title="Copy your API key"
        description="Send it as a bearer token when you call a published flow."
        onClose={onClose}
        footer={<Button onClick={onClose}>Done</Button>}
      >
        <div className="space-y-4">
          <WarnNote>
            This key will not be shown again. Store it somewhere safe before
            you press Done.
          </WarnNote>
          <div className="flex items-start gap-2">
            <code className="min-w-0 flex-1 rounded-xl border border-[var(--edge-strong)] bg-ink-900/70 px-3.5 py-3 font-mono text-sm leading-relaxed break-all text-ink-100 select-all">
              {issued}
            </code>
            <Button
              variant="secondary"
              className="flex-none"
              onClick={() => {
                void navigator.clipboard?.writeText(issued);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      title="New API key"
      description="For code that runs your published flows from outside this console."
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            loading={busy}
            disabled={!trimmed}
          >
            Create key
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
          placeholder="CI pipeline"
          hint="Say where the key lives, so you know what stops when it is revoked."
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <Field
          label="Expires in (days)"
          name="expires"
          inputMode="numeric"
          placeholder="Never"
          hint="Optional. Between 1 and 3650 days."
          value={days}
          onChange={(event) => setDays(event.target.value.replace(/\D/g, ""))}
        />
      </form>
    </Modal>
  );
}

/* -------------------------------------------------------------- revoke --- */

function RevokeDialog({
  orgId,
  apiKey,
  onClose,
  onRevoked,
}: {
  orgId: string;
  apiKey: ApiKey;
  onClose: () => void;
  onRevoked: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function revoke() {
    setBusy(true);
    setError(null);
    try {
      await api.del(`/api/v1/orgs/${orgId}/api-keys/${apiKey.id}`);
      onRevoked();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not revoke that key.",
      );
      setBusy(false);
    }
  }

  return (
    <Modal
      size="sm"
      title="Revoke API key"
      description="Calls that use this key start failing the moment it is revoked."
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
            onClick={() => void revoke()}
            loading={busy}
            className="hover:brightness-110"
            style={{
              background: "var(--status-bad)",
              color: "var(--color-ink-950)",
            }}
          >
            Revoke key
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <div className="flex items-center gap-3 rounded-xl border border-[var(--edge)] bg-ink-900/50 px-4 py-3">
          <IconChip size="sm">
            <KeyIcon />
          </IconChip>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-ink-100">
              {apiKey.name}
            </p>
            <p className="font-mono text-xs text-ink-400">{apiKey.prefix}…</p>
          </div>
        </div>
        <p className="text-sm leading-relaxed text-ink-300">
          Anything still sending it, such as a CI job or another service, is
          rejected from then on. The key stays in the list as revoked, for
          audit. This cannot be undone.
        </p>
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------- how to call --- */

function HowToCall() {
  const [base, setBase] = useState("");

  useEffect(() => {
    void loadConfig().then((config) => setBase(config.public_base_url ?? ""));
  }, []);

  const runUrl = `${base || "https://YOUR_HOST"}/flows/FLOW_ID/run`;

  return (
    <Card className="p-5">
      <div className="flex items-start gap-3.5">
        <IconChip hue="var(--color-accent-500)" className="max-sm:hidden">
          <TerminalIcon />
        </IconChip>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-ink-100">
            How to call a flow
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-ink-400">
            Send the key as a bearer token to the Run URL of a published flow.
            Open the flow in the builder and use Call this flow to copy its
            exact URL.
          </p>
          <pre className="mt-3.5 overflow-x-auto rounded-xl border border-[var(--edge)] bg-ink-900/70 px-4 py-3.5 font-mono text-xs leading-relaxed text-ink-200">
            <code>
              {`curl -X POST ${runUrl} \\\n  -H "Authorization: Bearer `}
              <span className="text-brand-300">bsv_YOUR_API_KEY</span>
              {`" \\\n  -H "Content-Type: application/json" \\\n  -d '{"input": {"name": "Ada"}}'`}
            </code>
          </pre>
        </div>
      </div>
    </Card>
  );
}

/* --------------------------------------------------------------- pieces --- */

/** Alert's shape in the warn colour, which Alert itself does not offer. */
function WarnNote({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      className="flex items-start gap-2.5 rounded-xl border border-[color-mix(in_oklab,var(--status-warn)_45%,transparent)] bg-[color-mix(in_oklab,var(--status-warn)_12%,transparent)] px-3.5 py-2.5 text-sm text-[var(--status-warn)]"
    >
      <svg
        viewBox="0 0 24 24"
        className="mt-0.5 h-4 w-4 flex-none"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="M12 8v5M12 16.5v.5M12 3l9 16H3l9-16Z"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="min-w-0">{children}</span>
    </div>
  );
}

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

function RevokeIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M6.5 6.5l11 11"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TerminalIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect
        x="3"
        y="4.5"
        width="18"
        height="15"
        rx="2.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        d="M7.5 9l3 3-3 3M12.5 15h4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
