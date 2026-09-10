/**
 * Tools and MCP servers, defined once for the whole workspace.
 *
 * They used to live inside the agent node that called them, which is right for
 * exactly one agent. The moment a second one needs the same "look up the
 * order" call the definition gets copied, and from then on the two drift: one
 * gets the fix, the other does not, and nobody can answer "which flows call
 * our orders API" without opening every node on every canvas.
 *
 * So this screen owns them and the agent refers to them. The same argument
 * applies twice over to MCP servers, which carry a URL and a credential:
 * rotating a key should be one edit, not six.
 *
 * Two lists rather than two pages. They are the same idea from an agent's side
 * — "things it can call" — and splitting them into separate screens would mean
 * checking two places to answer one question.
 */

import { useCallback, useEffect, useState, type FormEvent } from "react";

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
import { CheckBox, Select } from "../../components/fields";
import { PageHeader, RelativeTime } from "./bits";

export interface ToolRow {
  id: string;
  name: string;
  description: string;
  kind: "http" | "code" | "constant";
  definition: Record<string, unknown>;
  updated_at: string;
}

export interface McpRow {
  id: string;
  name: string;
  url: string;
  credential_id: string;
  headers: Record<string, string>;
  tools: string[];
  enabled: boolean;
  updated_at: string;
}

type Dialog =
  | { kind: "tool"; tool: ToolRow | null }
  | { kind: "mcp"; server: McpRow | null };

const INPUT =
  "w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 outline-none transition-colors focus:border-brand-400";


/** A label, a control, and the line under it. `Field` in ui.tsx is itself an
 *  input, so anything that is not one needs this instead. */
function Labelled({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="block text-sm font-medium text-ink-200">{label}</label>
      {children}
      {hint && (
        <p className="text-xs leading-relaxed text-ink-500">{hint}</p>
      )}
    </div>
  );
}

export function Toolbox() {
  const { orgId } = useWorkspace();
  const [tools, setTools] = useState<ToolRow[] | null>(null);
  const [servers, setServers] = useState<McpRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      const [toolRows, serverRows] = await Promise.all([
        api.get<ToolRow[]>(`/api/v1/orgs/${orgId}/tools`),
        api.get<McpRow[]>(`/api/v1/orgs/${orgId}/mcp-servers`),
      ]);
      setTools(toolRows);
      setServers(serverRows);
      setError(null);
    } catch (err) {
      // A 401 is the session ending, and the session handler is already
      // taking them to the sign-in screen. Saying the data failed sends
      // somebody looking for a problem that is not there.
      if (isSessionEnded(err)) return;
      setError("Could not load the tool library.");
      setTools([]);
      setServers([]);
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!orgId) return null;

  async function remove(what: "tools" | "mcp-servers", id: string) {
    try {
      await api.del(`/api/v1/orgs/${orgId}/${what}/${id}`);
      await load();
    } catch {
      setError("Could not delete that.");
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Build"
        title="Tools and MCP"
        subtitle="Define a tool once here, and every agent that uses it changes when you fix it."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              onClick={() => setDialog({ kind: "mcp", server: null })}
            >
              Add MCP server
            </Button>
            <Button onClick={() => setDialog({ kind: "tool", tool: null })}>
              New tool
            </Button>
          </div>
        }
      />

      {error && <Alert tone="error">{error}</Alert>}

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-ink-200">Tools</h2>
        {tools?.length === 0 && (
          <EmptyState
            title="No shared tools yet"
            action={
              <Button onClick={() => setDialog({ kind: "tool", tool: null })}>
                New tool
              </Button>
            }
          >
            A tool is one thing an agent can call: an HTTP request to your own
            API, a small Python function, or a fixed value while you are still
            wiring things up.
          </EmptyState>
        )}
        <ul className="space-y-2">
          {(tools ?? []).map((tool) => (
            <li
              key={tool.id}
              className="surface flex flex-wrap items-start gap-3 rounded-2xl px-4 py-3"
            >
              <IconChip>{tool.kind === "code" ? "PY" : tool.kind === "http" ? "API" : "=" }</IconChip>
              <div className="min-w-0 flex-1">
                <p className="font-mono text-sm text-ink-100">{tool.name}</p>
                <p className="mt-1 text-sm leading-relaxed text-ink-400">
                  {tool.description}
                </p>
                <p className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-ink-500">
                  <Pill>{tool.kind}</Pill>
                  {typeof tool.definition.url === "string" && tool.definition.url && (
                    <span className="truncate font-mono">
                      {String(tool.definition.method ?? "POST")}{" "}
                      {String(tool.definition.url)}
                    </span>
                  )}
                  <RelativeTime value={tool.updated_at} />
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  onClick={() => setDialog({ kind: "tool", tool })}
                >
                  Edit
                </Button>
                <Button variant="ghost" onClick={() => void remove("tools", tool.id)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-ink-200">MCP servers</h2>
        {servers?.length === 0 && (
          <EmptyState
            title="No MCP servers yet"
            action={
              <Button
                variant="secondary"
                onClick={() => setDialog({ kind: "mcp", server: null })}
              >
                Add MCP server
              </Button>
            }
          >
            An MCP server is somebody else&apos;s set of tools, reached over
            HTTP. Add it once with its key, and any agent can be given it.
          </EmptyState>
        )}
        <ul className="space-y-2">
          {(servers ?? []).map((server) => (
            <li
              key={server.id}
              className="surface flex flex-wrap items-start gap-3 rounded-2xl px-4 py-3"
            >
              <IconChip>MCP</IconChip>
              <div className="min-w-0 flex-1">
                <p className="font-mono text-sm text-ink-100">{server.name}</p>
                <p className="mt-1 truncate font-mono text-xs text-ink-500">
                  {server.url}
                </p>
                <p className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-ink-500">
                  {!server.enabled && <Pill tone="warn">Switched off</Pill>}
                  {server.tools.length > 0 && (
                    <span>{server.tools.length} tools allowed</span>
                  )}
                  <RelativeTime value={server.updated_at} />
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  onClick={() => setDialog({ kind: "mcp", server })}
                >
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => void remove("mcp-servers", server.id)}
                >
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      {dialog?.kind === "tool" && (
        <ToolDialog
          orgId={orgId}
          tool={dialog.tool}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            void load();
          }}
        />
      )}
      {dialog?.kind === "mcp" && (
        <McpDialog
          orgId={orgId}
          server={dialog.server}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            void load();
          }}
        />
      )}
    </div>
  );
}


function ToolDialog({
  orgId,
  tool,
  onClose,
  onSaved,
}: {
  orgId: string;
  tool: ToolRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const definition = tool?.definition ?? {};
  const [name, setName] = useState(tool?.name ?? "");
  const [description, setDescription] = useState(tool?.description ?? "");
  const [kind, setKind] = useState<ToolRow["kind"]>(tool?.kind ?? "http");
  const [url, setUrl] = useState(String(definition.url ?? ""));
  const [method, setMethod] = useState(String(definition.method ?? "POST"));
  const [code, setCode] = useState(String(definition.code ?? ""));
  const [value, setValue] = useState(
    definition.value === undefined ? "" : JSON.stringify(definition.value),
  );
  const [schema, setSchema] = useState(
    JSON.stringify(
      definition.input_schema ?? { type: "object", properties: {} },
      null,
      2,
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(schema);
      } catch {
        throw new ApiError(400, "The arguments schema is not valid JSON.");
      }
      const body: Record<string, unknown> = {
        name,
        description,
        kind,
        input_schema: parsed,
      };
      if (kind === "http") {
        body.url = url;
        body.method = method;
      }
      if (kind === "code") body.code = code;
      if (kind === "constant") {
        try {
          body.value = value ? JSON.parse(value) : null;
        } catch {
          body.value = value;
        }
      }
      const path = `/api/v1/orgs/${orgId}/tools${tool ? `/${tool.id}` : ""}`;
      if (tool) await api.patch(path, body);
      else await api.post(path, body);
      onSaved();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save that tool.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={tool ? "Edit tool" : "New tool"}
      onClose={onClose}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy}>
            Save
          </Button>
        </div>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        {error && <Alert tone="error">{error}</Alert>}

        <Field
          label="Name"
          hint="What the model calls it. Lowercase and underscores."
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="get_order"
        />

        <Labelled
          label="When should an agent use it?"
          hint="The model reads this and nothing else before deciding to call it."
        >
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={2}
            placeholder="Look up one order by its number. Use it whenever a customer mentions an order."
            className={INPUT}
          />
        </Labelled>

        <Labelled label="What it does">
          <Select
            value={kind}
            onChange={(next) => setKind(next as ToolRow["kind"])}
            options={[
              {
                value: "http",
                label: "Call an HTTP endpoint",
                hint: "Your own API, or anyone's.",
              },
              {
                value: "code",
                label: "Run Python",
                hint: "A function, in the same sandbox the Run Python node uses.",
              },
              {
                value: "constant",
                label: "Return a fixed value",
                hint: "A stub, while you wire the rest up.",
              },
            ]}
          />
        </Labelled>

        {kind === "http" && (
          <div className="flex gap-2">
            <Select
              className="w-32 flex-none"
              ariaLabel="Method"
              value={method}
              onChange={setMethod}
              options={["GET", "POST", "PUT", "PATCH", "DELETE"].map((item) => ({
                value: item,
                label: item,
              }))}
            />
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://api.example.com/orders/{{ tool.number }}"
              className={INPUT}
            />
          </div>
        )}

        {kind === "code" && (
          <Labelled
            label="Python"
            hint="def main(data): the model's arguments are at data['args']."
          >
            <textarea
              value={code}
              onChange={(event) => setCode(event.target.value)}
              rows={6}
              spellCheck={false}
              className={`${INPUT} font-mono text-xs`}
            />
          </Labelled>
        )}

        {kind === "constant" && (
          <Field
            label="Value"
            hint="JSON, or plain text."
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        )}

        <Labelled
          label="Arguments"
          hint="JSON Schema, handed to the model as written. Leave the default for a tool that takes nothing."
        >
          <textarea
            value={schema}
            onChange={(event) => setSchema(event.target.value)}
            rows={6}
            spellCheck={false}
            className={`${INPUT} font-mono text-xs`}
          />
        </Labelled>
      </form>
    </Modal>
  );
}

function McpDialog({
  orgId,
  server,
  onClose,
  onSaved,
}: {
  orgId: string;
  server: McpRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(server?.name ?? "");
  const [url, setUrl] = useState(server?.url ?? "");
  const [credentialId, setCredentialId] = useState(server?.credential_id ?? "");
  const [tools, setTools] = useState((server?.tools ?? []).join(", "));
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = {
        name,
        url,
        credential_id: credentialId,
        headers: server?.headers ?? {},
        tools: tools
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        enabled,
      };
      const path = `/api/v1/orgs/${orgId}/mcp-servers${server ? `/${server.id}` : ""}`;
      if (server) await api.patch(path, body);
      else await api.post(path, body);
      onSaved();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save that server.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={server ? "Edit MCP server" : "Add MCP server"}
      onClose={onClose}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={submit} loading={busy}>
            Save
          </Button>
        </div>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        {error && <Alert tone="error">{error}</Alert>}

        <Field
          label="Name"
          hint="A short handle. Its tools reach the model as name__tool, which keeps two servers offering search apart."
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="docs"
        />
        <Field
          label="URL"
          hint="The server's HTTP endpoint."
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://mcp.example.com/mcp"
        />
        <Field
          label="Credential"
          hint="Optional. The id of a saved credential, sent as Authorization: Bearer. The key itself never lands in a flow."
          value={credentialId}
          onChange={(event) => setCredentialId(event.target.value)}
          placeholder="Leave empty for an open server"
        />
        <Field
          label="Only these tools"
          hint="Comma separated. Empty means every tool the server offers."
          value={tools}
          onChange={(event) => setTools(event.target.value)}
          placeholder="search, fetch"
        />
        <CheckBox
          checked={enabled}
          onChange={setEnabled}
          label="In use"
          hint="Switch it off to stop every agent calling it, without editing a single flow."
        />
      </form>
    </Modal>
  );
}
