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
};

/** Model providers only — what the Agent node's LLM picker offers. The VCS
 *  hosts are credentials, not places to run a model. */
export const MODEL_PROVIDERS = PROVIDERS_INTERNAL();
function PROVIDERS_INTERNAL() {
  return Object.entries(PROVIDER_LABEL)
    .filter(([value]) => value !== "github" && value !== "gitlab")
    .map(([value, label]) => ({ value, label }));
}

export const VCS_PROVIDERS: { value: string; label: string }[] = [
  { value: "github", label: "GitHub" },
  { value: "gitlab", label: "GitLab" },
];

export const PROVIDERS: { value: string; label: string }[] = Object.entries(PROVIDER_LABEL).map(
  ([value, label]) => ({ value, label }),
);
