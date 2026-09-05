/**
 * Provider credentials — created once, picked by name inside an Agent node.
 *
 * The secret is entered exactly once, on this screen, and never appears again:
 * the API's `CredentialRead` schema has no field for it, so there is no
 * response this page could show it in even by accident. What is shown instead
 * is a `hint` — the key's last four characters — enough to tell two
 * credentials apart without exposing either.
 */

import { motion } from "motion/react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { ApiError, api } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Card, Field } from "../../components/ui";
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

export function Credentials() {
  const { orgId } = useWorkspace();
  const [credentials, setCredentials] = useState<Credential[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

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

  async function remove(credential: Credential) {
    if (
      !confirm(
        `Delete "${credential.name}"? Any agent node using it will fail until you point it at another credential.`,
      )
    )
      return;
    try {
      await api.del(`/api/v1/orgs/${orgId}/credentials/${credential.id}`);
      await load();
    } catch {
      setError("Could not delete that credential.");
    }
  }

  if (credentials === null) return null;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Account"
        title="Credentials"
        subtitle="Provider API keys for the AI Agent node. Stored encrypted, referenced by name, never embedded in a flow."
        action={
          <Button onClick={() => setCreating((value) => !value)}>
            {creating ? "Cancel" : "New credential"}
          </Button>
        }
      />

      {error && <Alert>{error}</Alert>}

      {creating && (
        <NewCredential
          orgId={orgId!}
          onCreated={() => {
            setCreating(false);
            void load();
          }}
        />
      )}

      {credentials.length === 0 && !creating ? (
        <Card className="p-10 text-center">
          <p className="text-ink-200">No credentials yet.</p>
          <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-ink-500">
            Add one for any provider: Anthropic, OpenAI, Google, Groq, Mistral
            and a dozen more. Every Agent node in this workspace can use it by
            name.
          </p>
        </Card>
      ) : (
        <ul className="space-y-2.5">
          {credentials.map((credential, index) => (
            <motion.li
              key={credential.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                delay: Math.min(index * 0.03, 0.25),
                duration: 0.25,
              }}
            >
              <div className="surface flex flex-wrap items-center gap-4 rounded-2xl p-5">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-3">
                    <p className="truncate font-medium text-ink-100">
                      {credential.name}
                    </p>
                    <span className="rounded-md border border-ink-700 px-1.5 py-0.5 text-[0.68rem] text-ink-400">
                      {PROVIDER_LABEL[credential.provider] ??
                        credential.provider}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-xs text-ink-500">
                    {credential.hint ? `…${credential.hint}` : "no key preview"}
                  </p>
                </div>
                <div className="text-right text-xs text-ink-500">
                  <p>
                    Last used <RelativeTime value={credential.last_used_at} />
                  </p>
                  <p className="mt-1">
                    Created <RelativeTime value={credential.created_at} />
                  </p>
                </div>
                <Button variant="ghost" onClick={() => void remove(credential)}>
                  Delete
                </Button>
              </div>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface TestResult {
  supported: boolean;
  models: string[];
  error: string | null;
}

function NewCredential({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [provider, setProvider] = useState(PROVIDERS[0].value);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<TestResult | null>(null);

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
          base_url: baseUrl.trim() || null,
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
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/v1/orgs/${orgId}/credentials`, {
        name: name.trim(),
        provider,
        api_key: apiKey,
        base_url: baseUrl.trim() || null,
      });
      onCreated();
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
    <Card className="p-6">
      <form onSubmit={submit} className="max-w-md space-y-4" noValidate>
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
        <div>
          <label className="mb-1.5 block text-sm font-medium text-ink-300">
            Provider
          </label>
          <select
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value);
              invalidateTest();
            }}
            className="w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 outline-none focus:border-brand-400"
          >
            {PROVIDERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <Field
          label="API key"
          name="api_key"
          type="password"
          required
          autoComplete="off"
          placeholder="Pasted once, never shown again"
          value={apiKey}
          onChange={(event) => {
            setApiKey(event.target.value);
            invalidateTest();
          }}
        />
        {provider === "anthropic" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-500">
            On a Claude Pro or Max plan with no API key? Run{" "}
            <code className="rounded bg-ink-850 px-1 py-0.5 font-mono text-[0.7rem] text-ink-200">
              claude setup-token
            </code>{" "}
            on your own computer and paste the token it prints, which starts
            with sk-ant-oat01. It signs in Claude Code for Fix Code and Open PR.
            The AI Agent node still needs a real API key.
          </p>
        )}
        {provider === "mcp" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-500">
            The token an MCP server expects. An agent node that names the
            server sends it as Authorization: Bearer. There is nothing to test
            it against until a node says which server, so Test connection is
            skipped for this provider.
          </p>
        )}
        {provider === "jira" && (
          <p className="-mt-2 text-xs leading-relaxed text-ink-500">
            Write the key as{" "}
            <code className="rounded bg-ink-850 px-1 py-0.5 font-mono text-[0.7rem] text-ink-200">
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

        <div className="flex gap-2">
          <Button
            type="button"
            variant="secondary"
            onClick={() => void testConnection()}
            loading={testing}
            disabled={!apiKey.trim()}
          >
            Test connection
          </Button>
          <Button
            type="submit"
            loading={busy}
            disabled={!name.trim() || !apiKey.trim()}
          >
            Save credential
          </Button>
        </div>
      </form>
    </Card>
  );
}
