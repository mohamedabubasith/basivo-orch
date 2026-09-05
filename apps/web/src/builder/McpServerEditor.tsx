/**
 * MCP servers for an agent — one card per server, plain fields.
 *
 * Same stored shape as `McpServer` in `basivo_orch/flows/nodes/mcp.py`: a
 * name, an HTTP URL, an optional credential (sent as a bearer token), and an
 * optional list of tool names. Command-line servers are deliberately not
 * offered; the API refuses them too.
 */

import { useState } from "react";

import { CredentialPicker } from "./pickers";

export interface McpServerValue {
  name: string;
  url: string;
  credential_id?: string;
  headers?: Record<string, string>;
  tools?: string[];
}

const INPUT =
  "w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2 text-sm text-ink-100 " +
  "outline-none transition-colors focus:border-brand-400";

export function McpServerEditor({
  value,
  onChange,
  orgId,
}: {
  value: unknown;
  onChange: (servers: McpServerValue[]) => void;
  orgId?: string | null;
}) {
  const servers: McpServerValue[] = Array.isArray(value)
    ? (value as McpServerValue[])
    : [];
  const [open, setOpen] = useState<number | null>(null);

  function update(index: number, patch: Partial<McpServerValue>) {
    onChange(
      servers.map((server, i) =>
        i === index ? { ...server, ...patch } : server,
      ),
    );
  }

  function remove(index: number) {
    onChange(servers.filter((_, i) => i !== index));
    setOpen(null);
  }

  return (
    <div className="space-y-2">
      {servers.map((server, index) => (
        <div
          key={index}
          className="relative rounded-lg border border-ink-700/70 bg-ink-950/40"
        >
          <button
            type="button"
            onClick={() => setOpen(open === index ? null : index)}
            className="flex w-full items-center gap-2 py-2 pr-16 pl-3 text-left"
          >
            <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-200">
              {server.name || "unnamed"}
            </span>
            <span className="max-w-[45%] truncate text-[0.62rem] text-ink-500">
              {server.url || "no URL yet"}
            </span>
          </button>
          <button
            type="button"
            aria-label={`Remove MCP server ${server.name}`}
            title="Remove this server"
            onClick={() => remove(index)}
            className="absolute top-1.5 right-2 rounded-md p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-[var(--status-bad)]"
          >
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none">
              <path
                d="M6 6l12 12M18 6L6 18"
                stroke="currentColor"
                strokeWidth="1.8"
              />
            </svg>
          </button>

          {open === index && (
            <div className="space-y-2.5 border-t border-ink-800/70 p-3">
              <label className="block">
                <span className="mb-1 block text-[0.68rem] text-ink-400">
                  Name
                </span>
                <input
                  value={server.name}
                  onChange={(e) =>
                    update(index, {
                      name: e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""),
                    })
                  }
                  placeholder="docs"
                  className={INPUT}
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[0.68rem] text-ink-400">
                  URL
                </span>
                <input
                  value={server.url}
                  onChange={(e) => update(index, { url: e.target.value })}
                  placeholder="https://mcp.example.com/mcp"
                  className={INPUT}
                />
              </label>
              <div>
                <span className="mb-1 block text-[0.68rem] text-ink-400">
                  Credential (optional, sent as a bearer token)
                </span>
                <CredentialPicker
                  orgId={orgId}
                  provider="mcp"
                  value={server.credential_id ?? ""}
                  onChange={(v) => update(index, { credential_id: v })}
                />
              </div>
              <label className="block">
                <span className="mb-1 block text-[0.68rem] text-ink-400">
                  Only these tools (comma separated, empty means all)
                </span>
                <input
                  value={(server.tools ?? []).join(", ")}
                  onChange={(e) =>
                    update(index, {
                      tools: e.target.value
                        .split(",")
                        .map((t) => t.trim())
                        .filter(Boolean),
                    })
                  }
                  placeholder="search, read_page"
                  className={INPUT}
                />
              </label>
            </div>
          )}
        </div>
      ))}

      <button
        type="button"
        onClick={() => {
          onChange([...servers, { name: "", url: "", tools: [] }]);
          setOpen(servers.length);
        }}
        className="w-full rounded-xl border border-dashed border-ink-700/70 px-3 py-2 text-xs text-ink-400 transition-colors hover:border-ink-500 hover:text-ink-200"
      >
        + Add an MCP server
      </button>
      <p className="text-[0.68rem] leading-relaxed text-ink-500">
        Servers are reached over HTTP. Save the server's token as a credential
        with provider MCP server, and pick it here; it never sits in the flow.
      </p>
    </div>
  );
}
