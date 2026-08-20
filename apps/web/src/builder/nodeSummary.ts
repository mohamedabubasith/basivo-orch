/**
 * The one line that makes a node card informative.
 *
 * "AI Agent / agent.llm" tells you what the node *is*, which you already knew
 * from its icon. On a canvas with four agents the question is which model each
 * one calls, which repository the fix targets, which channel the post goes to
 * — and answering that required opening every node in turn.
 *
 * Deliberately the most identifying field per type, not a config dump: a card
 * is glanceable or it is nothing.
 */

const str = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

/** Model ids are namespaced (`openai/gpt-oss-120b`); the tail identifies it. */
const shortModel = (model: string): string => model.split("/").pop() ?? model;

export function nodeSummary(
  type: string,
  config: Record<string, unknown>,
): string {
  switch (type) {
    case "agent.llm": {
      const model = shortModel(str(config.model));
      const team = Array.isArray(config.sub_agents)
        ? config.sub_agents.length
        : 0;
      const tools = Array.isArray(config.tools) ? config.tools.length : 0;
      const extras = [
        tools ? `${tools} tool${tools > 1 ? "s" : ""}` : "",
        team ? `${team} sub-agent${team > 1 ? "s" : ""}` : "",
        // Worth a card slot: whether an agent remembers changes what the same
        // prompt does on the second run, and it is invisible otherwise.
        config.memory === "conversation" ? "remembers" : "",
      ].filter(Boolean);
      return [model || "no model", ...extras].join(" · ");
    }
    case "git.autofix":
    case "git.ticket":
    case "git.comment":
      return str(config.repo) || "no repository";
    case "trigger.webhook": {
      const methods = Array.isArray(config.methods)
        ? config.methods.join("/")
        : "POST";
      return config.require_signature
        ? `${methods} · signed`
        : `${methods} · unsigned`;
    }
    case "trigger.schedule":
      return config.mode === "cron"
        ? str(config.cron) || "no cron set"
        : config.interval_seconds
          ? `every ${config.interval_seconds}s`
          : "no interval set";
    case "logic.condition": {
      const count = Array.isArray(config.comparisons)
        ? config.comparisons.length
        : 0;
      const match = str(config.match) || "all";
      return count
        ? `${count} check${count > 1 ? "s" : ""} · match ${match}`
        : "no checks";
    }
    case "data.set": {
      const assignments = Array.isArray(config.assignments)
        ? config.assignments
        : [];
      const names = assignments
        .map((entry) =>
          entry && typeof entry === "object"
            ? str((entry as never)["name"])
            : "",
        )
        .filter(Boolean);
      return names.length
        ? names.slice(0, 3).join(", ") + (names.length > 3 ? "…" : "")
        : "nothing set";
    }
    case "http.request":
      return `${str(config.method) || "GET"} ${str(config.url) || "no URL"}`;
    case "code.python": {
      const code = str(config.code);
      return code ? `${code.split("\n").length} lines` : "no code";
    }
    case "design.render":
      return str(config.size) || "instagram_square";
    case "video.render":
      return `${str(config.template) || "custom"} · ${str(config.quality) || "standard"}`;
    case "video.generate":
      return `${config.duration_seconds ?? 6}s · ${str(config.size) || "landscape"} · ${
        shortModel(str(config.model)) || "no model"
      }`;
    case "social.post":
      return [str(config.platform) || "telegram", str(config.target)]
        .filter(Boolean)
        .join(" · ");
    case "trigger.manual":
      return "started by hand";
    default:
      return "";
  }
}
