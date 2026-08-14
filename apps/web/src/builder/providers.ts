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
};

export const PROVIDERS: { value: string; label: string }[] = Object.entries(PROVIDER_LABEL).map(
  ([value, label]) => ({ value, label }),
);
