/**
 * The node configuration panel, generated from the API's JSON Schema.
 *
 * The alternative was a hand-written form per node type, which fails in a
 * specific and expensive way: the engine gains a node, the palette shows it
 * because the palette is already generated, and the panel renders nothing to
 * configure it with. Reading `config_schema` means a node added to `_NODES` in
 * Python is fully editable here without a line of TypeScript.
 *
 * The trade is that the forms are plain. That is the right trade for six node
 * types that are still changing; a node whose editing experience genuinely
 * needs bespoke UI can be special-cased later, and the generic path stays as
 * the floor rather than the ceiling.
 */

import { useRef, useState } from "react";

import { cx } from "../lib/cx";
import { NodeIconChip } from "./nodeIcons";
import type { Suggestion } from "./suggestions";
import { TemplateInput } from "./TemplateInput";
import { ExpandButton, ExpandDialog } from "./ExpandField";
import { CredentialPicker, ModelPicker, RepoPicker, SkillPicker } from "./pickers";
import { ApiError, api } from "../lib/api";
import { SubAgentEditor } from "./SubAgentEditor";
import { MODEL_PROVIDERS, VCS_PROVIDERS, VOICES } from "./providers";
import { ToolEditor } from "./ToolEditor";
import { McpServerEditor } from "./McpServerEditor";
import type { NodeSpec } from "./specs";
import { NodeGuide } from "./NodeGuide";

interface SchemaField {
  key: string;
  title: string;
  description?: string;
  type: string;
  enum?: string[];
  /** What a person reads for each enum value; from the schema's x-enum-labels. */
  enumLabels?: Record<string, string>;
  /** Tucked behind "Advanced settings"; from the schema's x-advanced. */
  advanced?: boolean;
  /** Edited by a bespoke panel, never by the generic form; from x-hidden. */
  hidden?: boolean;
  required: boolean;
  itemDef?: JsonSchema;
  default?: unknown;
  /** From the schema, and the signal for whether a field wants a big editor. */
  maxLength?: number;
}

export interface JsonSchema {
  "x-enum-labels"?: Record<string, string>;
  "x-advanced"?: boolean;
  "x-hidden"?: boolean;
  "x-pattern-hint"?: string;
  pattern?: string;
  type?: string;
  title?: string;
  description?: string;
  enum?: string[];
  default?: unknown;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  anyOf?: JsonSchema[];
  additionalProperties?: boolean | JsonSchema;
  maxLength?: number;
}

/**
 * Resolve `$ref`, and collapse the `anyOf: [T, null]` pydantic emits for
 * optionals.
 *
 * Pydantic puts `title` and `description` on the *outer* `X | None` wrapper,
 * not on its concrete branch — `{"anyOf": [{"type": "number"}, {"type":
 * "null"}], "title": "Temperature"}"`. Keeping only the concrete branch (as an
 * earlier version of this did) discards that wrapper and with it the title —
 * every optional field on every node rendered labelled by its raw snake_case
 * key instead of its name. The outer schema's `title`/`description` win when
 * present; the concrete branch's are the fallback for the plain, non-optional
 * case this function also handles.
 */
function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (schema.$ref) {
    const name = schema.$ref.replace("#/$defs/", "");
    return resolve(root.$defs?.[name] ?? {}, root);
  }
  if (schema.anyOf) {
    const concrete = schema.anyOf.find((option) => option.type !== "null");
    if (concrete) {
      const resolved = resolve(concrete, root);
      return {
        ...resolved,
        title: schema.title ?? resolved.title,
        description: schema.description ?? resolved.description,
        default: schema.default,
      };
    }
  }
  return schema;
}

/** `instagram_square` is an identifier; "Instagram square" is a label. Values
 * without underscores (mp4, 9:16, high) are already words and stay as they are. */
function readable(value: string): string {
  if (!value.includes("_")) return value;
  const words = value.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function fields(root: JsonSchema): SchemaField[] {
  const required = new Set(root.required ?? []);
  return Object.entries(root.properties ?? {}).map(([key, raw]) => {
    const schema = resolve(raw, root);
    return {
      key,
      title: schema.title ?? key,
      description: schema.description,
      type: schema.type ?? "string",
      enum: schema.enum,
      enumLabels: schema["x-enum-labels"],
      advanced: schema["x-advanced"] === true,
      hidden: schema["x-hidden"] === true,
      required: required.has(key),
      itemDef: schema.items ? resolve(schema.items, root) : undefined,
      default: schema.default,
      maxLength: schema.maxLength,
    };
  });
}

export function Inspector({
  spec,
  name,
  config,
  problem,
  orgId,
  flowId,
  publicBase,
  isPublished,
  nextRunAt,
  suggestions = [],
  onRename,
  onChange,
  onDelete,
  onClose,
}: {
  spec: NodeSpec;
  name: string;
  config: Record<string, unknown>;
  problem?: string;
  /** Only needed for the Agent node's credential picker. */
  orgId?: string | null;
  /** For nodes whose config is about being called from outside — the webhook
      trigger prints its own invocation URL rather than making the user hunt
      for it. */
  flowId?: string;
  publicBase?: string;
  isPublished?: boolean;
  /** When the scheduler will next fire this flow, if it is armed. */
  nextRunAt?: string | null;
  /** What {{ … }} can refer to from this node — see suggestions.ts. */
  suggestions?: Suggestion[];
  onRename: (name: string) => void;
  onChange: (config: Record<string, unknown>) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const schema = spec.config_schema as JsonSchema;
  const list = fields(schema);
  const isAgent = spec.type === "agent.llm";
  // The autofix node embeds an LLM config under the same field names the
  // Agent uses, so the provider/model/credential pickers apply to both.
  // Derived from the schema, not a list of node types. It was a list, and the
  // Video Generator — which calls a model exactly like the others — was left
  // off it, so its provider rendered as a free-text box and its credential
  // asked for a UUID nobody can supply. Any node carrying both a `provider`
  // and a `credential_id` gets the pickers, so the next one is right by
  // default rather than by remembering.
  const [expanded, setExpanded] = useState<SchemaField | null>(null);
  // Settings most people never touch stay out of the way until asked for.
  // Anything the schema marks advanced is hidden, not removed: one click
  // shows all of them, in place, in their normal order.
  const [showAdvanced, setShowAdvanced] = useState(false);
  const advancedCount = list.filter((field) => field.advanced).length;
  const keys = new Set(list.map((field) => field.key));
  const usesLlm = keys.has("provider") && keys.has("credential_id");
  const usesGit =
    spec.type === "git.ticket" ||
    spec.type === "git.autofix" ||
    spec.type === "git.comment";

  // Several set() calls in one handler must compose. Spreading the `config`
  // prop gave each call the snapshot from before the handler ran, so choosing
  // a ticket source (template, issue number, ticket provider: three writes)
  // kept only the last one. Found by the QA plugin, not by eye.
  const latest = useRef(config);
  latest.current = config;
  function set(key: string, value: unknown) {
    const next = { ...latest.current };
    // Deleting rather than storing undefined: the API applies its own defaults,
    // and an explicit null would override one.
    if (value === undefined || value === "") delete next[key];
    else next[key] = value;
    latest.current = next;
    onChange(next);
  }

  return (
    <aside
      data-testid="node-settings"
      className="flex h-full w-full flex-col bg-ink-900/60"
    >
      <div className="flex items-center gap-3 border-b border-ink-800/70 px-5 py-4">
        <NodeIconChip type={spec.type} size={8} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-ink-100">
            {spec.label}
          </p>
          <p className="truncate text-xs text-ink-500">
            {spec.description}
          </p>
        </div>
        <button
          onClick={onClose}
          aria-label="Close node settings"
          className="rounded-lg p-1.5 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
        >
          <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
            <path
              d="M6 6l12 12M18 6L6 18"
              stroke="currentColor"
              strokeWidth="1.8"
            />
          </svg>
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
        <details className="group rounded-xl border border-ink-800/70 bg-ink-900/40 px-3 py-2">
          <summary className="cursor-pointer list-none text-xs text-ink-400 transition-colors hover:text-ink-200">
            <span className="mr-1.5 inline-block transition-transform group-open:rotate-90">
              ›
            </span>
            About this node
          </summary>
          <div className="pt-3">
            <NodeGuide spec={spec} />
          </div>
        </details>

        {problem && (
          <p
            className="rounded-lg border p-2.5 text-xs leading-relaxed"
            style={{
              borderColor:
                "color-mix(in oklab, var(--status-bad) 45%, transparent)",
              color: "var(--status-bad)",
            }}
          >
            {problem}
          </p>
        )}

        {spec.type === "trigger.schedule" && (
          <div className="rounded-lg border border-ink-700/70 bg-ink-950/40 p-3">
            <p className="text-xs font-medium text-ink-300">
              This schedule
            </p>
            {nextRunAt ? (
              <>
                <p className="mt-1.5 font-mono text-xs text-ink-100">
                  Next run {new Date(nextRunAt).toLocaleString()}
                </p>
                <p className="mt-1.5 text-xs leading-relaxed text-ink-500">
                  Armed. The run worker fires it. Nothing needs to call this
                  flow. Cron is read in the timezone below, so 6am stays 6am
                  across daylight saving.
                </p>
              </>
            ) : (
              <p className="mt-1 text-xs leading-relaxed text-ink-500">
                {isPublished
                  ? "Not armed yet. Publish again after setting the schedule, and the next run time appears here."
                  : "A schedule only runs once the flow is published. Publish, and the next run time appears here."}
              </p>
            )}
          </div>
        )}

        {spec.type === "trigger.webhook" && (
          <WebhookSource
            config={config}
            set={set}
            orgId={orgId}
            flowId={flowId}
            publicBase={publicBase}
            isPublished={isPublished}
          />
        )}

        <Labelled label="Name" hint="Shown on the canvas and in the run log.">
          <input
            value={name}
            onChange={(event) => onRename(event.target.value)}
            className="w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 outline-none focus:border-brand-400"
          />
        </Labelled>

        {expanded && (
          <ExpandDialog
            title={expanded.title}
            hint={expanded.description}
            onClose={() => setExpanded(null)}
          >
            <textarea
              value={String(config[expanded.key] ?? "")}
              onChange={(event) => set(expanded.key, event.target.value)}
              spellCheck={false}
              autoFocus
              className={cx(
                "h-[60vh] w-full resize-none rounded-2xl border border-ink-700 bg-ink-950/60 p-4",
                "text-ink-100 outline-none focus:border-brand-400",
                CODE_FIELDS.has(expanded.key)
                  ? "font-mono text-[0.8rem] leading-relaxed"
                  : "text-sm leading-relaxed",
              )}
            />
          </ExpandDialog>
        )}

        {list
          .filter((field) => !field.hidden)
          .filter((field) => showAdvanced || !field.advanced)
          .filter(
            (field) =>
              // While memory is off there is no thread to key or bound, so the
              // two fields that configure one would be asking about nothing.
              !(
                isAgent &&
                (field.key === "memory_key" || field.key === "memory_window") &&
                (config.memory ?? "off") === "off"
              ) &&
              // A budget for skills nobody selected is a number about nothing.
              !(
                field.key === "skill_budget_chars" &&
                !(Array.isArray(config.skills) && config.skills.length > 0)
              ) &&
              // Voice, speed and captions describe narration that is switched
              // off — three controls for something that will not happen.
              !(
                spec.type === "video.generate" &&
                (field.key === "voice" ||
                  field.key === "voice_speed" ||
                  field.key === "captions") &&
                !config.narration
              ),
          )
          .map((field) => (
            <Labelled
              key={field.key}
              label={field.title}
              required={field.required}
              hint={field.description}
              onExpand={
                isLongText(field) ? () => setExpanded(field) : undefined
              }
            >
              {spec.type === "trigger.webhook" && field.key === "methods" ? (
                <MethodPicker
                  value={
                    Array.isArray(config.methods)
                      ? (config.methods as string[])
                      : ["POST"]
                  }
                  onChange={(methods) => set("methods", methods)}
                />
              ) : isAgent && field.key === "provider" ? (
                <select
                  value={String(config.provider ?? MODEL_PROVIDERS[0].value)}
                  onChange={(event) => set("provider", event.target.value)}
                  className={INPUT}
                >
                  {MODEL_PROVIDERS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
        {advancedCount > 0 && (
          <button
            type="button"
            onClick={() => setShowAdvanced((open) => !open)}
            className="w-full rounded-xl border border-dashed border-ink-700/70 px-3 py-2 text-xs text-ink-400 transition-colors hover:border-ink-500 hover:text-ink-200"
          >
            {showAdvanced
              ? "Hide advanced settings"
              : `Show advanced settings (${advancedCount})`}
          </button>
        )}
                </select>
              ) : usesLlm &&
                (field.key === "credential_id" ||
                  field.key === "vision_credential_id") ? (
                // Both LLM credentials get the picker. Without this the vision
                // one renders as a text box asking for a UUID, which is not a
                // thing any person can supply.
                <CredentialPicker
                  orgId={orgId}
                  provider={String(
                    (field.key === "vision_credential_id"
                      ? config.vision_provider
                      : null) ??
                      config.provider ??
                      MODEL_PROVIDERS[0].value,
                  )}
                  value={String(config[field.key] ?? "")}
                  onChange={(v) => set(field.key, v)}
                />
              ) : spec.type === "video.render" && field.key === "html" ? (
                <CodeArea
                  value={String(config.html ?? "")}
                  onChange={(v) => set("html", v)}
                />
              ) : spec.type === "video.render" && field.key === "variables" ? (
                <div>
                  <TemplateInput
                    multiline
                    rows={4}
                    value={String(config.variables ?? "{}")}
                    onChange={(v) => set("variables", v)}
                    suggestions={suggestions}
                    placeholder={'{"headline": "{{ nodes.copy.output.text }}"}'}
                  />
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-500">
                    JSON, filled into the template. An upstream agent usually
                    writes these. That is the division of labour: it writes
                    words, the template does layout.
                  </p>
                </div>
              ) : spec.type === "design.render" && field.key === "html" ? (
                <CodeArea
                  value={String(config.html ?? "")}
                  onChange={(v) => set("html", v)}
                  placeholder={
                    '<div style="width:1080px;height:1080px;display:grid;place-items:center;background:#111;color:#fff;font:700 72px Inter">\n  {{ nodes.copy.output.headline }}\n</div>'
                  }
                />
              ) : spec.type === "social.post" &&
                field.key === "credential_id" ? (
                <CredentialPicker
                  orgId={orgId}
                  provider={String(config.platform ?? "telegram")}
                  value={String(config.credential_id ?? "")}
                  onChange={(v) => set("credential_id", v)}
                />
              ) : spec.type === "social.post" &&
                (field.key === "text" ||
                  field.key === "artifact_id" ||
                  field.key === "target") ? (
                <TemplateInput
                  multiline={field.key === "text"}
                  rows={3}
                  value={String(config[field.key] ?? "")}
                  onChange={(v) => set(field.key, v)}
                  suggestions={suggestions}
                  placeholder={
                    field.key === "artifact_id"
                      ? "{{ nodes.poster.output.artifact_id }}"
                      : ""
                  }
                />
              ) : spec.type === "code.python" && field.key === "code" ? (
                <CodeArea
                  value={String(config.code ?? "")}
                  onChange={(v) => set("code", v)}
                />
              ) : field.key === "voice" ? (
                <select
                  value={String(config.voice ?? "af_heart")}
                  onChange={(event) => set("voice", event.target.value)}
                  className={INPUT}
                >
                  {VOICES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : field.key === "skills" ? (
                <SkillPicker
                  orgId={orgId}
                  value={
                    Array.isArray(config.skills)
                      ? (config.skills as string[])
                      : []
                  }
                  onChange={(skills) => set("skills", skills)}
                />
              ) : isAgent && field.key === "memory" ? (
                <div>
                  <select
                    value={String(config.memory ?? "off")}
                    onChange={(event) => set("memory", event.target.value)}
                    className={INPUT}
                  >
                    <option value="off">Off (every run starts fresh)</option>
                    <option value="conversation">
                      Remember the conversation
                    </option>
                  </select>
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-500">
                    {config.memory === "conversation"
                      ? "Past requests and replies are sent again before the new one, so you can say “that didn’t work” and be understood. Tool calls are never stored."
                      : "Right for one-shot work: classifying or summarising whatever arrives. Remembering would only bias it."}
                  </p>
                </div>
              ) : isAgent && field.key === "memory_key" ? (
                <div>
                  <TemplateInput
                    value={String(config.memory_key ?? "")}
                    onChange={(v) => set("memory_key", v)}
                    suggestions={suggestions}
                    placeholder="{{ input.body.issue.number }}"
                  />
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-500">
                    One separate thread per value, usually an issue number or a
                    chat id.{" "}
                    {config.memory_key
                      ? ""
                      : "Left empty, every trigger shares one thread, so two people would read each other’s history."}
                  </p>
                </div>
              ) : isAgent && field.key === "team_mode" ? (
                <div>
                  <select
                    value={String(config.team_mode ?? "delegate")}
                    onChange={(event) => set("team_mode", event.target.value)}
                    className={INPUT}
                  >
                    <option value="delegate">
                      Delegate: it asks, then answers itself
                    </option>
                    <option value="handover">
                      Handover: it transfers, they answer you
                    </option>
                  </select>
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-500">
                    {config.team_mode === "handover"
                      ? "Control moves. The agent it transfers to replies directly and can transfer on again. Right for triage."
                      : "This agent stays in charge: it asks, gets an answer back, and writes the reply itself. Right for combining several answers."}
                  </p>
                </div>
              ) : isAgent && field.key === "sub_agents" ? (
                <SubAgentEditor
                  value={config.sub_agents}
                  onChange={(agents) => set("sub_agents", agents)}
                  orgId={orgId}
                  parentProvider={String(
                    config.provider ?? MODEL_PROVIDERS[0].value,
                  )}
                  parentCredentialId={String(config.credential_id ?? "")}
                  teamMode={String(config.team_mode ?? "delegate")}
                />
              ) : isAgent && field.key === "tools" ? (
                <ToolEditor
                  value={config.tools}
                  onChange={(tools) => set("tools", tools)}
                  suggestions={suggestions}
                />
              ) : field.key === "mcp_servers" ? (
                <McpServerEditor
                  value={config.mcp_servers}
                  onChange={(servers) => set("mcp_servers", servers)}
                  orgId={orgId}
                />
              ) : usesLlm && field.key === "vision_provider" ? (
                <select
                  value={String(config.vision_provider ?? "")}
                  onChange={(event) =>
                    set("vision_provider", event.target.value)
                  }
                  className={INPUT}
                >
                  <option value="">Same as the repair model</option>
                  {MODEL_PROVIDERS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : usesGit && field.key === "repo" ? (
                <RepoPicker
                  orgId={orgId}
                  credentialId={String(config.git_credential_id ?? "")}
                  value={String(config.repo ?? "")}
                  onChange={(v) => set("repo", v)}
                />
              ) : spec.type === "git.autofix" && field.key === "problem" ? (
                <div className="space-y-2">
                  <ProblemSource
                    value={String(config.problem ?? "")}
                    onChange={(v) => set("problem", v)}
                    onIssueNumber={(v) => set("issue_number", v)}
                    onTicketProvider={(v) => set("ticket_provider", v)}
                    suggestions={suggestions}
                  />
                  {config.ticket_provider === "jira" && (
                    <Labelled
                      label="Jira credential"
                      hint="Used to post the pull request link back on the ticket."
                    >
                      <CredentialPicker
                        orgId={orgId}
                        provider="jira"
                        value={String(config.ticket_credential_id ?? "")}
                        onChange={(v) => set("ticket_credential_id", v)}
                      />
                    </Labelled>
                  )}
                </div>
              ) : usesGit && field.key === "git_provider" ? (
                <select
                  value={String(config.git_provider ?? "github")}
                  onChange={(event) => set("git_provider", event.target.value)}
                  className={INPUT}
                >
                  {VCS_PROVIDERS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : usesGit && field.key === "git_credential_id" ? (
                <CredentialPicker
                  orgId={orgId}
                  provider={String(config.git_provider ?? "github")}
                  value={String(config.git_credential_id ?? "")}
                  onChange={(v) => set("git_credential_id", v)}
                />
              ) : (spec.type === "git.autofix" &&
                  (field.key === "instructions" ||
                    field.key === "issue_number")) ||
                (spec.type === "git.comment" && field.key === "body") ? (
                <TemplateInput
                  multiline={field.key !== "issue_number"}
                  rows={3}
                  value={String(config[field.key] ?? "")}
                  onChange={(v) => set(field.key, v)}
                  suggestions={suggestions}
                  placeholder={
                    field.key === "issue_number"
                      ? "{{ input.body.issue.number }}, or leave empty"
                      : ""
                  }
                />
              ) : isAgent &&
                (field.key === "prompt" || field.key === "system") ? (
                <TemplateInput
                  multiline
                  rows={field.key === "prompt" ? 5 : 3}
                  value={String(config[field.key] ?? "")}
                  onChange={(v) => set(field.key, v)}
                  suggestions={suggestions}
                  placeholder={
                    field.key === "prompt" ? "{{ input.text }}" : "You are…"
                  }
                />
              ) : usesLlm &&
                (field.key === "model" || field.key === "vision_model") ? (
                <ModelPicker
                  orgId={orgId}
                  credentialId={String(
                    (field.key === "vision_model"
                      ? config.vision_credential_id
                      : null) ??
                      config.credential_id ??
                      "",
                  )}
                  value={String(config[field.key] ?? "")}
                  onChange={(v) => set(field.key, v)}
                />
              ) : (
                <FieldInput
                  field={field}
                  value={config[field.key]}
                  onChange={(v) => set(field.key, v)}
                  suggestions={suggestions}
                />
              )}
            </Labelled>
          ))}
      </div>

      <div className="flex items-center justify-between gap-3 border-t border-ink-800/70 px-5 py-3">
        <button
          onClick={onDelete}
          className="rounded-lg border border-ink-700 px-3 py-2 text-sm transition-colors hover:border-[var(--status-bad)]"
          style={{ color: "var(--status-bad)" }}
        >
          Delete node
        </button>
        {/* Changes are applied as they are typed, so one button: Done. */}
        <button
          onClick={onClose}
          className="rounded-lg bg-brand-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-400"
        >
          Done
        </button>
      </div>
    </aside>
  );
}

function Labelled({
  label,
  hint,
  required,
  onExpand,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  onExpand?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <label className="block text-[0.7rem] font-medium tracking-wide text-ink-300">
          {label}
          {required && <span style={{ color: "var(--status-warn)" }}> *</span>}
        </label>
        {onExpand && <ExpandButton onClick={onExpand} label={label} />}
      </div>
      {children}
      {hint && (
        <p className="mt-1 text-xs leading-relaxed text-ink-500">
          {hint}
        </p>
      )}
    </div>
  );
}

/** Fields whose content is code or markup, and want a monospace editor. */
const CODE_FIELDS = new Set(["code", "html", "variables", "instructions"]);

/**
 * Whether a field deserves the expand button.
 *
 * By capacity, not by name: anything the API will accept a thousand characters
 * of is something a person may well write a thousand characters into, and the
 * inspector rail is 356px wide. Naming the fields individually would mean the
 * next long field silently arrives cramped.
 */
function isLongText(field: SchemaField): boolean {
  if (field.type !== "string" || field.enum) return false;
  return CODE_FIELDS.has(field.key) || (field.maxLength ?? 0) >= 1000;
}

const INPUT =
  "w-full rounded-xl border border-ink-700 bg-ink-950/60 px-3 py-2.5 text-sm text-ink-100 " +
  "outline-none transition-colors focus:border-brand-400 focus:ring-2 focus:ring-brand-400/15";

/**
 * A concrete example rather than an empty `[]`. The Agent node's `tools`
 * field is an array of objects with its own nested schema — the generic
 * form here does not build a sub-form per tool, so what stands in for one is
 * a worked example a person can copy and edit, showing every field a tool
 * can carry: an HTTP call, and the constant-value stub used to fake one.
 */
const TOOLS_EXAMPLE = JSON.stringify(
  [
    {
      name: "get_weather",
      description: "Look up the current weather for a city.",
      input_schema: {
        type: "object",
        properties: { city: { type: "string" } },
        required: ["city"],
      },
      kind: "http",
      url: "https://api.example.com/weather?city={{ tool.city }}",
      method: "GET",
    },
    {
      name: "stub_example",
      description:
        "Always returns the same value, useful while testing a flow.",
      input_schema: { type: "object", properties: {} },
      kind: "constant",
      value: "ok",
    },
  ],
  null,
  1,
);

function FieldInput({
  field,
  value,
  onChange,
  suggestions = [],
}: {
  field: SchemaField;
  value: unknown;
  onChange: (value: unknown) => void;
  suggestions?: Suggestion[];
}) {
  if (field.enum) {
    return (
      <select
        value={String(value ?? field.default ?? field.enum[0])}
        onChange={(event) => onChange(event.target.value)}
        className={INPUT}
      >
        {field.enum.map((option) => (
          <option key={option} value={option}>
            {field.enumLabels?.[option] ?? readable(option)}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "boolean") {
    const checked = Boolean(value ?? field.default);
    return (
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          onClick={() => onChange(!checked)}
          className={cx(
            "relative h-6 w-11 flex-none rounded-full border transition-colors",
            // ink-600 + an ink-100 knob, not ink-700 + white: in light mode
            // ink-700 is #dfe3ed and a white knob on it is invisible — the
            // switch read as an empty pill with no state at all.
            checked
              ? "border-brand-500 bg-brand-500"
              : "border-ink-500 bg-ink-600",
          )}
        >
          <span
            className={cx(
              // left-0 is load-bearing: without it the knob starts from the
              // button's centred static position and the slide carries it
              // clean out of the pill, over the On/Off word.
              "absolute top-[3px] left-0 h-4 w-4 rounded-full transition-transform",
              checked ? "translate-x-6 bg-white" : "translate-x-1 bg-ink-100",
            )}
          />
        </button>
        {/* The state in a word, so the toggle is never a colour-only guess. */}
        <span
          className={cx("text-xs", checked ? "text-ink-200" : "text-ink-500")}
        >
          {checked ? "On" : "Off"}
        </span>
      </div>
    );
  }

  if (field.type === "integer" || field.type === "number") {
    return (
      <input
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        placeholder={
          field.default === undefined || field.default === null
            ? ""
            : String(field.default)
        }
        onChange={(event) =>
          onChange(
            event.target.value === "" ? undefined : Number(event.target.value),
          )
        }
        className={INPUT}
      />
    );
  }

  // Objects and arrays are edited as JSON. A bespoke row editor per shape is
  // the eventual answer; until then this is at least honest about what it is,
  // and it refuses to hand back invalid JSON rather than saving a broken config.
  if (field.type === "object" || field.type === "array") {
    return (
      <JsonInput
        value={value}
        placeholder={
          field.key === "tools"
            ? TOOLS_EXAMPLE
            : field.type === "array"
              ? "[]"
              : "{}"
        }
        onChange={onChange}
      />
    );
  }

  return (
    <TemplateInput
      value={value === undefined || value === null ? "" : String(value)}
      placeholder={
        field.default === undefined || field.default === null
          ? ""
          : String(field.default)
      }
      onChange={(v) => onChange(v)}
      suggestions={suggestions}
      mono
    />
  );
}

function JsonInput({
  value,
  placeholder,
  onChange,
}: {
  value: unknown;
  placeholder: string;
  onChange: (value: unknown) => void;
}) {
  const [text, setText] = useState(() =>
    value === undefined || value === null ? "" : JSON.stringify(value, null, 2),
  );
  const [invalid, setInvalid] = useState(false);

  return (
    <>
      <textarea
        value={text}
        rows={4}
        spellCheck={false}
        placeholder={placeholder}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          if (next.trim() === "") {
            setInvalid(false);
            onChange(undefined);
            return;
          }
          try {
            onChange(JSON.parse(next));
            setInvalid(false);
          } catch {
            // Held back deliberately: committing unparseable text would either
            // save a broken config or silently discard what was typed.
            setInvalid(true);
          }
        }}
        className={cx(
          INPUT,
          "resize-y font-mono text-[0.75rem]",
          invalid &&
            "border-[var(--status-bad)] focus:border-[var(--status-bad)]",
        )}
      />
      {invalid && (
        <p
          className="mt-1 text-xs"
          style={{ color: "var(--status-bad)" }}
        >
          Not valid JSON. The last valid value is still saved.
        </p>
      )}
    </>
  );
}

function CodeArea({
  value,
  onChange,
  placeholder = 'def main(data):\n    # data has: input, nodes, vars, trigger\n    return {"ok": True}',
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      rows={16}
      spellCheck={false}
      autoCorrect="off"
      autoCapitalize="off"
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        event.preventDefault();
        const target = event.currentTarget;
        const { selectionStart, selectionEnd } = target;
        const next =
          value.slice(0, selectionStart) + "    " + value.slice(selectionEnd);
        onChange(next);
        // The value update is async through React; restore the caret after it.
        requestAnimationFrame(() => {
          target.selectionStart = target.selectionEnd = selectionStart + 4;
        });
      }}
      className={cx(
        INPUT,
        "resize-y font-mono text-[0.78rem] leading-relaxed whitespace-pre",
      )}
    />
  );
}

/**
 * HTTP methods as checkboxes. The generic form rendered this list-of-enums as
 * a JSON textarea — asking someone to type ["POST","PUT"] by hand, quotes and
 * all, to answer a five-option multiple choice.
 */
function MethodPicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (methods: string[]) => void;
}) {
  const ALL = ["GET", "POST", "PUT", "PATCH", "DELETE"];
  return (
    <div className="flex flex-wrap gap-1.5">
      {ALL.map((method) => {
        const active = value.includes(method);
        return (
          <button
            key={method}
            type="button"
            aria-pressed={active}
            onClick={() => {
              const next = active
                ? value.filter((m) => m !== method)
                : [...value, method];
              // Zero methods is a webhook nothing can call; refuse the last
              // uncheck rather than saving a config that can never fire.
              if (next.length > 0) onChange(next);
            }}
            className={cx(
              "rounded-lg border px-2.5 py-1.5 font-mono text-[0.7rem] transition-colors",
              active
                ? "border-brand-400 bg-brand-500/15 text-ink-100"
                : "border-ink-700 text-ink-400 hover:border-ink-500",
            )}
          >
            {method}
          </button>
        );
      })}
    </div>
  );
}

/** Where the text of the problem comes from, as choices rather than a template. */
const PROBLEM_SOURCES: {
  value: string;
  label: string;
  template: string;
  /** Where that source keeps its issue number, so the node can report back. */
  issueNumber?: string;
  /** Set when the ticket lives outside the git host, so the report goes there. */
  ticketProvider?: string;
}[] = [
  {
    value: "github_issue",
    label: "The GitHub issue that triggered the webhook",
    template: "{{ input.body.issue.title }}\n\n{{ input.body.issue.body }}",
    issueNumber: "{{ input.body.issue.number }}",
  },
  {
    value: "gitlab_issue",
    label: "The GitLab issue that triggered the webhook",
    template:
      "{{ input.body.object_attributes.title }}\n\n{{ input.body.object_attributes.description }}",
    issueNumber: "{{ input.body.object_attributes.iid }}",
  },
  {
    value: "jira_issue",
    label: "The Jira ticket that triggered the webhook",
    template: "{{ trigger.ticket.title }}\n\n{{ trigger.ticket.description }}",
    issueNumber: "{{ trigger.ticket.key }}",
    ticketProvider: "jira",
  },
  {
    value: "webhook_body",
    label: "Everything the webhook sent",
    template: "{{ input.body }}",
  },
  {
    value: "previous",
    label: "Whatever the previous node produced",
    template: "{{ input }}",
  },
];

function ProblemSource({
  value,
  onChange,
  onIssueNumber,
  onTicketProvider,
  suggestions,
}: {
  value: string;
  onChange: (value: string) => void;
  onIssueNumber: (value: string) => void;
  onTicketProvider: (value: string) => void;
  suggestions: Suggestion[];
}) {
  const preset = PROBLEM_SOURCES.find((source) => source.template === value);
  const [custom, setCustom] = useState(value !== "" && !preset);
  const mode = custom ? "custom" : (preset?.value ?? "");

  return (
    <div className="space-y-2">
      <select
        value={mode}
        onChange={(event) => {
          const next = event.target.value;
          if (next === "custom") {
            setCustom(true);
            return;
          }
          setCustom(false);
          const chosen = PROBLEM_SOURCES.find((source) => source.value === next);
          onChange(chosen ? chosen.template : "");
          // The issue number rides along, so "comment on the issue when
          // done" works without anyone knowing where the number lives.
          onIssueNumber(chosen?.issueNumber ?? "");
          onTicketProvider(chosen?.ticketProvider ?? "");
        }}
        className={INPUT}
      >
        <option value="">Where is the problem described?</option>
        {PROBLEM_SOURCES.map((source) => (
          <option key={source.value} value={source.value}>
            {source.label}
          </option>
        ))}
        <option value="custom">I will write it, or mix in my own words</option>
      </select>
      {custom && (
        <TemplateInput
          multiline
          rows={4}
          value={value}
          onChange={onChange}
          suggestions={suggestions}
          placeholder="Describe the bug, or reference earlier nodes with {{ }}"
        />
      )}
      {!custom && preset && (
        <p className="text-xs leading-relaxed text-ink-500">
          The agent receives:{" "}
          <code className="font-mono">{preset.template.replace(/\n\n/g, " ")}</code>
        </p>
      )}
    </div>
  );
}

const GITHUB_EVENTS: { value: string; label: string }[] = [
  { value: "issues", label: "An issue is opened or edited" },
  { value: "issue_comment", label: "Someone comments on an issue" },
  { value: "pull_request", label: "A pull request is opened or updated" },
  { value: "push", label: "Code is pushed" },
];

/** Mirrors `JIRA_HOOK_EVENTS` in `nodes/jira.py`. */
const JIRA_EVENTS: { value: string; label: string }[] = [
  { value: "jira:issue_created", label: "A ticket is created" },
  { value: "jira:issue_updated", label: "A ticket is updated" },
  { value: "comment_created", label: "Someone comments on a ticket" },
];

/**
 * Where a webhook's calls come from. Two answers: "a GitHub repository", in
 * which case this platform registers the webhook at GitHub itself when the
 * person presses Connect; or "anything that can POST", in which case they
 * get the URL and paste it wherever they like. Nobody is shown a secret in
 * the first case, because a derived one is used and checked on both ends.
 */
function WebhookSource({
  config,
  set,
  orgId,
  flowId,
  publicBase,
  isPublished,
}: {
  config: Record<string, unknown>;
  set: (key: string, value: unknown) => void;
  orgId?: string | null;
  flowId?: string;
  publicBase?: string;
  isPublished?: boolean;
}) {
  const provider = String(config.listen_provider ?? "");
  const credential = String(config.listen_credential_id ?? "");
  const repo = String(config.listen_repo ?? "");
  const filter = String(config.listen_filter ?? "");
  const events = Array.isArray(config.listen_events)
    ? (config.listen_events as string[])
    : provider === "jira"
      ? ["jira:issue_created"]
      : ["issues"];
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  function choose(next: string) {
    set("listen_provider", next);
    // Event names belong to one system; a GitHub list left behind on a Jira
    // choice would register nothing Jira understands.
    set("listen_events", next === "jira" ? ["jira:issue_created"] : ["issues"]);
    set("listen_credential_id", "");
  }

  async function connect() {
    if (!orgId || !flowId) return;
    setBusy(true);
    setNote(null);
    try {
      if (provider === "jira") {
        const result = await api.post<{ site: string; events: string[]; updated: boolean }>(
          `/api/v1/orgs/${orgId}/flows/${flowId}/jira/connect`,
          { credential_id: credential, filter, events },
        );
        setNote({
          ok: true,
          text: `${result.updated ? "Updated" : "Connected"}. ${result.site} now calls this flow when ${describe(result.events, JIRA_EVENTS)}.`,
        });
      } else {
        const result = await api.post<{ repo: string; events: string[]; updated: boolean }>(
          `/api/v1/orgs/${orgId}/flows/${flowId}/github/connect`,
          { credential_id: credential, repo, events },
        );
        setNote({
          ok: true,
          text: `${result.updated ? "Updated" : "Connected"}. GitHub now calls this flow when ${describe(result.events)} in ${result.repo}.`,
        });
      }
    } catch (err) {
      setNote({
        ok: false,
        text:
          err instanceof ApiError
            ? err.message
            : `Could not connect to ${provider === "jira" ? "Jira" : "GitHub"}.`,
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-ink-700/70 bg-ink-950/40 p-3">
      <Labelled label="Where do calls come from?">
        <select
          value={provider}
          onChange={(event) => choose(event.target.value)}
          className={INPUT}
        >
          <option value="">Anything that can POST. I will paste the URL myself.</option>
          <option value="github">A GitHub repository. Set it up for me.</option>
          <option value="jira">A Jira site. Set it up for me.</option>
        </select>
      </Labelled>

      {provider === "jira" ? (
        <>
          <Labelled label="Jira credential">
            <CredentialPicker
              orgId={orgId}
              provider="jira"
              value={credential}
              onChange={(v) => set("listen_credential_id", v)}
            />
          </Labelled>
          <Labelled
            label="Which tickets"
            hint="A JQL filter. Leave empty for every project the account can see."
          >
            <input
              value={filter}
              onChange={(event) => set("listen_filter", event.target.value)}
              placeholder="project = OPS"
              className={INPUT}
            />
          </Labelled>
          <Labelled label="Start this flow when">
            <div className="space-y-1.5">
              {JIRA_EVENTS.map((event) => (
                <label key={event.value} className="flex items-center gap-2 text-xs text-ink-200">
                  <input
                    type="checkbox"
                    checked={events.includes(event.value)}
                    onChange={(e) =>
                      set(
                        "listen_events",
                        e.target.checked
                          ? [...events, event.value]
                          : events.filter((v) => v !== event.value),
                      )
                    }
                  />
                  {event.label}
                </label>
              ))}
            </div>
          </Labelled>
          <p className="text-xs leading-relaxed text-ink-500">
            Nothing to do in Jira. When you publish, the webhook is registered on
            the site for you (the credential has to be a Jira administrator), and
            re-registered on every publish.
          </p>
          {isPublished && (
            <button
              type="button"
              onClick={() => void connect()}
              disabled={busy || !credential || events.length === 0}
              className="w-full rounded-xl border border-ink-600 px-3 py-2 text-xs font-medium text-ink-200 transition-colors hover:border-brand-400 disabled:opacity-40"
            >
              {busy ? "Connecting…" : "Reconnect now"}
            </button>
          )}
          {note && (
            <p
              className="text-xs leading-relaxed"
              style={{ color: note.ok ? "var(--status-good)" : "var(--status-bad)" }}
            >
              {note.text}
            </p>
          )}
          <p className="text-xs leading-relaxed text-ink-500">
            The ticket arrives flattened as{" "}
            <code className="text-ink-400">{"{{ input.ticket.title }}"}</code>,{" "}
            <code className="text-ink-400">{"{{ input.ticket.description }}"}</code> and{" "}
            <code className="text-ink-400">{"{{ input.ticket.key }}"}</code>. On Fix Code
            and Open PR, choose “The Jira ticket that triggered the webhook”.
          </p>
        </>
      ) : provider === "github" ? (
        <>
          <Labelled label="GitHub credential">
            <CredentialPicker
              orgId={orgId}
              provider="github"
              value={credential}
              onChange={(v) => set("listen_credential_id", v)}
            />
          </Labelled>
          <Labelled label="Repository">
            <RepoPicker
              orgId={orgId}
              credentialId={credential}
              value={repo}
              onChange={(v) => set("listen_repo", v)}
            />
          </Labelled>
          <Labelled label="Start this flow when">
            <div className="space-y-1.5">
              {GITHUB_EVENTS.map((event) => (
                <label key={event.value} className="flex items-center gap-2 text-xs text-ink-200">
                  <input
                    type="checkbox"
                    checked={events.includes(event.value)}
                    onChange={(e) =>
                      set(
                        "listen_events",
                        e.target.checked
                          ? [...events, event.value]
                          : events.filter((v) => v !== event.value),
                      )
                    }
                  />
                  {event.label}
                </label>
              ))}
            </div>
          </Labelled>
          <p className="text-xs leading-relaxed text-ink-500">
            Nothing to do on GitHub. When you publish, the webhook is registered
            on the repository for you, and re-registered on every publish.
          </p>
          {isPublished && (
            <button
              type="button"
              onClick={() => void connect()}
              disabled={busy || !credential || !repo || events.length === 0}
              className="w-full rounded-xl border border-ink-600 px-3 py-2 text-xs font-medium text-ink-200 transition-colors hover:border-brand-400 disabled:opacity-40"
            >
              {busy ? "Connecting…" : "Reconnect now"}
            </button>
          )}
          {note && (
            <p
              className="text-xs leading-relaxed"
              style={{ color: note.ok ? "var(--status-good)" : "var(--status-bad)" }}
            >
              {note.text}
            </p>
          )}
          <p className="text-xs leading-relaxed text-ink-500">
            The delivery arrives as <code className="text-ink-400">{"{{ input.body }}"}</code>.
            For an issue, the title is{" "}
            <code className="text-ink-400">{"{{ input.body.issue.title }}"}</code>.
          </p>
        </>
      ) : (
        <>
          <p className="text-xs font-medium text-ink-300">This webhook's URL</p>
          {isPublished ? (
            <code className="block truncate rounded-md bg-ink-950/60 px-2 py-1.5 font-mono text-xs text-ink-200">
              {publicBase}/hooks/{flowId}
            </code>
          ) : (
            <p className="text-xs leading-relaxed text-ink-500">
              Appears after you publish. An unpublished flow has no stable URL
              for callers to depend on.
            </p>
          )}
          <p className="text-xs leading-relaxed text-ink-500">
            Turn on Require signature and set a secret; callers send it as{" "}
            <code className="text-ink-400">X-Webhook-Secret</code> (GitLab:{" "}
            <code className="text-ink-400">X-Gitlab-Token</code>). The delivery
            arrives as <code className="text-ink-400">{"{{ input.body }}"}</code>.
          </p>
        </>
      )}
    </div>
  );
}

function describe(events: string[], known = GITHUB_EVENTS): string {
  const names = events.map(
    (value) => known.find((e) => e.value === value)?.label.toLowerCase() ?? value,
  );
  return names.length <= 1 ? names[0] ?? "something happens" : names.slice(0, -1).join(", ") + " or " + names[names.length - 1];
}

