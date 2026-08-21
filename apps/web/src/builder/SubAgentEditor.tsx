/**
 * Editing the agents an agent may hand work to.
 *
 * Agent-to-agent, made configurable. Each entry becomes a tool on the parent —
 * `ask_<name>` in delegate mode, `transfer_to_<name>` in handover — so the
 * parent decides *at run time* who to involve, which is the thing wiring two
 * Agent nodes on the canvas cannot do: that pipeline is fixed when you draw
 * it.
 *
 * Deliberately shaped like the tool editor: a collapsed row per agent with the
 * delete control on the header (never buried inside), expanding to its
 * settings. Two editors that look unrelated make one screen feel like two.
 */

import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import { cx } from "../lib/cx";
import { CredentialPicker, ModelPicker } from "./pickers";
import { MODEL_PROVIDERS } from "./providers";

export interface SubAgentValue {
  name: string;
  description?: string;
  system?: string;
  provider?: string;
  model?: string;
  credential_id?: string;
  temperature?: number | null;
  max_tokens?: number;
  max_iterations?: number;
}

const INPUT =
  "w-full rounded-lg border border-ink-700 bg-ink-950/60 px-2.5 py-2 text-sm text-ink-100 outline-none focus:border-brand-400";
const LABEL = "mb-1 block text-[0.68rem] font-medium text-ink-400";

export function SubAgentEditor({
  value,
  onChange,
  orgId,
  parentProvider,
  parentCredentialId,
  teamMode,
}: {
  value: unknown;
  onChange: (agents: SubAgentValue[]) => void;
  orgId?: string | null;
  parentProvider: string;
  parentCredentialId: string;
  /** Decides the tool each entry becomes, so the row shows the real name. */
  teamMode: string;
}) {
  const agents: SubAgentValue[] = Array.isArray(value)
    ? (value as SubAgentValue[])
    : [];
  const [open, setOpen] = useState<number | null>(null);
  // The row names the tool the parent will actually see. Showing `ask_` while
  // the mode generates `transfer_to_` is a small lie that costs someone an
  // afternoon when their system prompt names the wrong tool.
  const prefix = teamMode === "handover" ? "transfer_to_" : "ask_";

  function update(index: number, patch: Partial<SubAgentValue>) {
    onChange(
      agents.map((agent, i) => (i === index ? { ...agent, ...patch } : agent)),
    );
  }

  function remove(index: number) {
    onChange(agents.filter((_, i) => i !== index));
    setOpen(null);
  }

  function add() {
    onChange([
      ...agents,
      { name: `agent_${agents.length + 1}`, description: "" },
    ]);
    setOpen(agents.length);
  }

  return (
    <div className="space-y-2">
      <AnimatePresence initial={false}>
        {agents.map((agent, index) => (
          <motion.div
            key={index}
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="relative overflow-hidden rounded-lg border border-ink-700/70 bg-ink-950/40"
          >
            <button
              type="button"
              onClick={() => setOpen(open === index ? null : index)}
              className="flex w-full items-center gap-2 py-2 pr-16 pl-3 text-left"
            >
              <span
                className="h-1.5 w-1.5 flex-none rounded-full"
                style={{ backgroundColor: "var(--color-brand-300)" }}
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-200">
                {prefix}
                {agent.name || "unnamed"}
              </span>
              <span className="flex-none text-[0.62rem] text-ink-500 uppercase">
                {agent.model ? "own model" : "inherits"}
              </span>
              <svg
                viewBox="0 0 24 24"
                className={cx(
                  "h-3.5 w-3.5 flex-none text-ink-500 transition-transform",
                  open === index && "rotate-180",
                )}
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M6 9l6 6 6-6"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
            </button>

            <button
              type="button"
              aria-label={`Remove sub-agent ${agent.name}`}
              title="Remove this agent"
              onClick={() => remove(index)}
              className="absolute top-1.5 right-8 rounded-md p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-[var(--status-bad)]"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-3.5 w-3.5"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M7 6.5l.8 12a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-12"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>

            {open === index && (
              <div className="space-y-3 border-t border-ink-700/70 p-3">
                <div>
                  <label className={LABEL}>Name</label>
                  <input
                    value={agent.name ?? ""}
                    onChange={(event) =>
                      update(index, {
                        name: event.target.value.replace(
                          /[^a-zA-Z0-9_-]/g,
                          "_",
                        ),
                      })
                    }
                    className={`${INPUT} font-mono`}
                  />
                  <p className="mt-1 text-[0.68rem] text-ink-500">
                    The parent calls it as{" "}
                    <code className="text-ink-400">
                      {prefix}
                      {agent.name || "name"}
                    </code>
                    {teamMode === "handover"
                      ? " and control moves to it."
                      : " and its answer comes back."}
                  </p>
                </div>

                <div>
                  <label className={LABEL}>What it is good at</label>
                  <textarea
                    rows={2}
                    value={agent.description ?? ""}
                    onChange={(event) =>
                      update(index, { description: event.target.value })
                    }
                    placeholder="Researches facts and returns short answers."
                    className={INPUT}
                  />
                  <p className="mt-1 text-[0.68rem] text-ink-500">
                    The parent reads this to decide when to involve it. Vague
                    here means it never does.
                  </p>
                </div>

                <div>
                  <label className={LABEL}>Its instructions</label>
                  <textarea
                    rows={3}
                    value={agent.system ?? ""}
                    onChange={(event) =>
                      update(index, { system: event.target.value })
                    }
                    placeholder="You are a careful researcher. Answer in one sentence."
                    className={INPUT}
                  />
                </div>

                <details className="rounded-lg border border-ink-700/70 p-2.5">
                  <summary className="cursor-pointer text-[0.68rem] text-ink-400">
                    Use a different model (defaults to the parent's)
                  </summary>
                  <div className="mt-3 space-y-3">
                    <div>
                      <label className={LABEL}>Provider</label>
                      <select
                        value={agent.provider ?? parentProvider}
                        onChange={(event) =>
                          update(index, { provider: event.target.value })
                        }
                        className={INPUT}
                      >
                        {MODEL_PROVIDERS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className={LABEL}>Credential</label>
                      <CredentialPicker
                        orgId={orgId}
                        provider={agent.provider ?? parentProvider}
                        value={agent.credential_id ?? ""}
                        onChange={(v) => update(index, { credential_id: v })}
                      />
                    </div>
                    <div>
                      <label className={LABEL}>Model</label>
                      <ModelPicker
                        orgId={orgId}
                        credentialId={agent.credential_id || parentCredentialId}
                        value={agent.model ?? ""}
                        onChange={(v) => update(index, { model: v })}
                      />
                    </div>
                    <div>
                      <label className={LABEL}>Max iterations</label>
                      <input
                        type="number"
                        min={1}
                        max={15}
                        value={agent.max_iterations ?? 4}
                        onChange={(event) =>
                          update(index, {
                            max_iterations: Number(event.target.value),
                          })
                        }
                        className={INPUT}
                      />
                    </div>
                  </div>
                </details>
              </div>
            )}
          </motion.div>
        ))}
      </AnimatePresence>

      <button
        type="button"
        onClick={add}
        className="w-full rounded-lg border border-dashed border-ink-700 py-2 text-xs text-ink-400 transition-colors hover:border-brand-400 hover:text-brand-300"
      >
        + Add an agent this one can ask
      </button>
    </div>
  );
}
