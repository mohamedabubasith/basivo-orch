/**
 * API keys for the external execution API.
 *
 * The screen is arranged around the one irreversible moment: the key is shown
 * exactly once, because only its hash is stored. There is no "reveal" here, and
 * saying so before the key exists is what stops someone closing the panel and
 * discovering the loss afterwards.
 */

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { ApiError, api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Card, Field, PageLoader } from "../../components/ui";
import { StatusPip } from "../../components/charts";
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

export function ApiKeys() {
  const { orgId } = useWorkspace();
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);

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

  async function revoke(key: ApiKey) {
    if (!confirm(`Revoke "${key.name}"? Anything using it stops working immediately.`)) return;
    try {
      await api.del(`/api/v1/orgs/${orgId}/api-keys/${key.id}`);
      await load();
    } catch {
      setError("Could not revoke that key.");
    }
  }

  if (keys === null) return <PageLoader label="Loading API keys" />;

  const live = keys.filter((key) => !key.revoked_at);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Account"
        title="API keys"
        subtitle="Run a published flow from your own code. Send the key as an Authorization: Bearer header."
        action={
          <Button
            onClick={() => {
              setCreating((value) => !value);
              setIssued(null);
            }}
          >
            {creating ? "Cancel" : "New key"}
          </Button>
        }
      />

      {error && <Alert>{error}</Alert>}

      <AnimatePresence>
        {issued && (
          <motion.div initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }}>
            <Card className="p-6">
              <h2 className="font-semibold text-ink-100">Copy your key now</h2>
              <p className="mt-1.5 text-sm leading-relaxed text-ink-400">
                This is the only time it is shown — the server kept a hash, so
                no screen can ever display it again. Losing it means issuing a
                new one.
              </p>
              <code className="mt-4 block rounded-xl border border-ink-700/70 bg-ink-950/60 p-3.5 font-mono text-sm break-all text-ink-200">
                {issued}
              </code>
              <div className="mt-4 flex gap-2">
                <Button
                  variant="secondary"
                  onClick={() => void navigator.clipboard?.writeText(issued)}
                >
                  Copy
                </Button>
                <Button onClick={() => setIssued(null)}>I have saved it</Button>
              </div>
            </Card>
          </motion.div>
        )}
      </AnimatePresence>

      {creating && (
        <NewKey
          orgId={orgId!}
          onCreated={(key) => {
            setCreating(false);
            setIssued(key);
            void load();
          }}
        />
      )}

      {live.length === 0 && !creating ? (
        <Card className="p-10 text-center">
          <p className="text-ink-200">No API keys.</p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-500">
            You only need one to call a flow from outside this app. Everything
            inside it uses your session instead.
          </p>
        </Card>
      ) : (
        <ul className="space-y-2.5">
          {keys.map((key) => (
            <li key={key.id} className="surface flex flex-wrap items-center gap-4 rounded-2xl p-5">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-3">
                  <p className="truncate font-medium text-ink-100">{key.name}</p>
                  {key.revoked_at ? (
                    <StatusPip tone="bad">Revoked</StatusPip>
                  ) : isExpired(key) ? (
                    <StatusPip tone="warn">Expired</StatusPip>
                  ) : (
                    <StatusPip tone="good">Active</StatusPip>
                  )}
                </div>
                <p className="mt-1 font-mono text-xs text-ink-500">{key.prefix}…</p>
              </div>
              <div className="text-right text-xs text-ink-500">
                <p>
                  Last used <RelativeTime value={key.last_used_at} />
                </p>
                <p className="mt-1">
                  Created <RelativeTime value={key.created_at} />
                </p>
              </div>
              {!key.revoked_at && (
                <Button variant="ghost" onClick={() => void revoke(key)}>
                  Revoke
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function isExpired(key: ApiKey): boolean {
  return key.expires_at !== null && new Date(key.expires_at).getTime() < Date.now();
}

function NewKey({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: (key: string) => void;
}) {
  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<{ key: string }>(`/api/v1/orgs/${orgId}/api-keys`, {
        name: name.trim(),
        expires_in_days: days ? Number(days) : null,
      });
      onCreated(created.key);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the key.");
      setBusy(false);
    }
  }

  return (
    <Card className="p-6">
      <form onSubmit={submit} className="max-w-md space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <Field
          label="Name"
          name="name"
          required
          autoFocus
          placeholder="CI pipeline"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <Field
          label="Expires in (days)"
          name="expires"
          inputMode="numeric"
          placeholder="Leave blank for no expiry"
          value={days}
          onChange={(event) => setDays(event.target.value.replace(/\D/g, ""))}
        />
        <Button type="submit" loading={busy} disabled={!name.trim()}>
          Create key
        </Button>
      </form>
    </Card>
  );
}
