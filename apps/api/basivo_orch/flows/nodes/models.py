"""Turning a saved credential into a LangChain chat model.

One fact shapes this whole module: **most providers speak OpenAI's API.** Of
the twenty-two the product offers, seventeen are OpenAI-compatible endpoints
that differ only in their base URL, so they all become `ChatOpenAI` pointed
somewhere else. Only Anthropic and Google need their own classes.

That is why a new model never needs a code change here — it is a string the
user picks from the catalogue their credential fetches live. It is also why
adding a provider is usually one line in `OPENAI_COMPATIBLE`.

The base URLs are not invented; they were read out of pydantic-ai's own
provider classes when this replaced it, so a provider that worked before
works now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from basivo_orch.flows.nodes.base import NodeContext, NodeError

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

#: provider -> the OpenAI-compatible endpoint it serves.
#:
#: An empty string means "OpenAI itself, or a base URL the credential
#: supplies" — Azure and Ollama are per-deployment, so their credential must
#: carry the URL.
OPENAI_COMPATIBLE: dict[str, str] = {
    "openai": "",
    "azure": "",
    "ollama": "",
    "deepseek": "https://api.deepseek.com",
    "moonshotai": "https://api.moonshot.ai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "xai": "https://api.x.ai/v1",
    "sambanova": "https://api.sambanova.ai/v1",
    "nebius": "https://api.studio.nebius.com/v1",
    "ovhcloud": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
    "alibaba": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "mistral": "https://api.mistral.ai/v1",
    "huggingface": "https://router.huggingface.co/v1",
}

#: Providers with a chat class of their own.
NATIVE = ("anthropic", "google")

SUPPORTED_PROVIDERS = tuple(sorted({*OPENAI_COMPATIBLE, *NATIVE}))


def _missing(provider: str, package: str) -> NodeError:
    return NodeError(
        f"The {provider} provider needs the {package} package, which is not installed. "
        f"Install it, or use an OpenAI-compatible endpoint by setting a base URL."
    )


async def build_chat_model(
    ctx: NodeContext,
    *,
    provider: str,
    model: str,
    credential_id: str,
    base_url: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    request_timeout: float | None = None,
    stop: list[str] | None = None,
) -> BaseChatModel:
    """Resolve a credential and return a ready chat model.

    Shared by every node that talks to a model, so a credential is decrypted
    in exactly one place and providers behave identically wherever they are
    used.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise NodeError(
            f"Unknown provider {provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if not model.strip():
        raise NodeError("No model selected. Pick one from the model list on the credential.")

    credential = None
    if credential_id:
        credential = await ctx.resolve_credential(credential_id)
        if credential is None:
            raise NodeError(
                "That credential no longer exists, or belongs to another workspace. "
                "Pick one again on this node."
            )

    api_key = credential.api_key if credential else ""
    resolved_base = base_url or (credential.base_url if credential else "") or ""

    common: dict[str, Any] = {"model": model}
    if temperature is not None:
        common["temperature"] = temperature
    if max_tokens is not None:
        common["max_tokens"] = max_tokens
    if top_p is not None:
        common["top_p"] = top_p
    if request_timeout is not None:
        common["timeout"] = request_timeout
    if stop:
        common["stop"] = stop

    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise _missing(provider, "langchain-anthropic") from exc
        if resolved_base:
            common["base_url"] = resolved_base
        return ChatAnthropic(api_key=api_key or None, **common)

    if provider == "google":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise _missing(provider, "langchain-google-genai") from exc
        common.pop("stop", None)
        return ChatGoogleGenerativeAI(google_api_key=api_key or None, **common)

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise _missing(provider, "langchain-openai") from exc

    endpoint = resolved_base or OPENAI_COMPATIBLE[provider]
    if not endpoint and provider != "openai":
        raise NodeError(
            f"{provider} needs a base URL. Set one on the credential. It is different "
            "for every deployment."
        )
    kwargs: dict[str, Any] = dict(common)
    if endpoint:
        kwargs["base_url"] = endpoint
    # An empty key would be sent as the literal string and rejected with a
    # confusing 401; None makes the client read the environment instead, which
    # is how self-hosted endpoints with no auth are reached.
    return ChatOpenAI(api_key=api_key or None, **kwargs)


def price_of(*, model: str, provider: str, input_tokens: int, output_tokens: int) -> float | None:
    """What a call cost, in USD, or None when the model's price is unknown.

    Cost per run is a product feature, not a nicety — it is on the run detail
    page next to the tokens. LangChain reports tokens but never money, so the
    conversion is done here with `genai-prices`, the same table that produced
    these figures before.
    """
    try:
        from genai_prices import calc_price
        from genai_prices.types import Usage
    except ImportError:  # pragma: no cover - optional dependency
        return None

    usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    for reference in (model, f"{provider}:{model}", model.split("/")[-1]):
        try:
            return float(calc_price(usage, model_ref=reference).total_price)
        except Exception:  # noqa: BLE001,S112 — an unknown model is not an error
            continue
    return None
