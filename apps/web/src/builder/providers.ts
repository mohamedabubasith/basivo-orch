/**
 * Providers a credential can be created for.
 *
 * Kept in sync by hand with `basivo_orch/credentials/schemas.py::PROVIDERS` —
 * both lists exist because the API validates against its own copy regardless
 * of what this one offers, so a mismatch fails loudly as a 422 rather than
 * silently accepting a provider the backend cannot construct.
 */

export const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  google: "Google (Gemini)",
  groq: "Groq",
  mistral: "Mistral",
  cohere: "Cohere",
  bedrock: "AWS Bedrock",
  azure: "Azure OpenAI",
  deepseek: "DeepSeek",
  xai: "xAI (Grok)",
  openrouter: "OpenRouter",
  together: "Together AI",
  fireworks: "Fireworks AI",
  cerebras: "Cerebras",
  huggingface: "Hugging Face",
  ollama: "Ollama",
  moonshotai: "Moonshot AI",
  zai: "Z.AI",
  sambanova: "SambaNova",
  nebius: "Nebius",
  ovhcloud: "OVHcloud",
  alibaba: "Alibaba Cloud",
  github: "GitHub (repos & issues)",
  gitlab: "GitLab (repos & issues)",
  jira: "Jira (tickets)",
  mcp: "MCP server (bearer token)",
};

/** Credentials that are not model providers: git hosts, ticket trackers, tool servers. */
export const NON_MODEL_PROVIDERS = new Set(["github", "gitlab", "jira", "mcp"]);

/** Model providers only — what the Agent node's LLM picker offers. The VCS
 *  hosts are credentials, not places to run a model. */
export const MODEL_PROVIDERS = PROVIDERS_INTERNAL();
function PROVIDERS_INTERNAL() {
  return Object.entries(PROVIDER_LABEL)
    .filter(([value]) => !NON_MODEL_PROVIDERS.has(value))
    .map(([value, label]) => ({ value, label }));
}

export const VCS_PROVIDERS: { value: string; label: string }[] = [
  { value: "github", label: "GitHub" },
  { value: "gitlab", label: "GitLab" },
];

export const PROVIDERS: { value: string; label: string }[] = Object.entries(
  PROVIDER_LABEL,
).map(([value, label]) => ({ value, label }));

/**
 * The voices offered for narration.
 *
 * A curated subset of the 54 the model ships: an undifferentiated list of ids
 * like `af_heart` / `bm_lewis` is not a choice a person can make, so each one
 * here says what it sounds like. Kept in step with `nodes/speech.py::VOICES`,
 * which is the list the API actually validates against.
 */
export const VOICES: { value: string; label: string }[] = [
  { value: "af_heart", label: "Heart (warm US female)" },
  { value: "af_bella", label: "Bella (bright US female)" },
  { value: "af_nicole", label: "Nicole (soft US female, close-mic)" },
  { value: "af_nova", label: "Nova (clear US female)" },
  { value: "am_michael", label: "Michael (steady US male)" },
  { value: "am_puck", label: "Puck (lively US male)" },
  { value: "am_onyx", label: "Onyx (deep US male)" },
  { value: "bf_emma", label: "Emma (UK female)" },
  { value: "bf_isabella", label: "Isabella (UK female, measured)" },
  { value: "bm_george", label: "George (UK male)" },
  { value: "bm_lewis", label: "Lewis (UK male, low)" },
  { value: "ef_dora", label: "Dora (Spanish female)" },
  { value: "ff_siwis", label: "Siwis (French female)" },
  { value: "hf_alpha", label: "Alpha (Hindi female)" },
  { value: "if_sara", label: "Sara (Italian female)" },
  { value: "jf_alpha", label: "Alpha (Japanese female)" },
  { value: "zf_xiaoxiao", label: "Xiaoxiao (Mandarin female)" },
];
