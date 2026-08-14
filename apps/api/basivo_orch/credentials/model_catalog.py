"""Live model lists per provider — and, as a side effect, a connectivity test.

Fetching a provider's own catalog needs exactly what running an agent against
it needs: a working key. So "does this key actually work" and "what models can
I pick from" are the same question, answered by the same call — if the list
comes back, the key is good. That is what backs the credential form's "Test
connection" and the Agent node's model dropdown.

Every provider whose pydantic-ai client wraps `AsyncOpenAI` or `AsyncAnthropic`
speaks the same `GET /models` shape — a `.data` list of objects with `.id` —
which, per `agent.PROVIDER_MODEL_MODULE`, is most of them: openai, azure,
deepseek, moonshotai, together, fireworks, cerebras, openrouter, xai,
sambanova, nebius, ovhcloud, alibaba, ollama, zai all go through
`AsyncOpenAI`, and anthropic through `AsyncAnthropic` with the same
`.data[].id` shape. Groq's client returns the list directly rather than
paginated, and Google's own SDK returns differently-shaped, differently-named
(`"models/gemini-..."`) results — both handled explicitly below.

Bedrock (AWS-signature auth, not a bearer key), Hugging Face (a model hub, not
a single inference catalog), Azure (deployments are user-named, not something
to list), Mistral and Cohere (SDKs whose list shape isn't the OpenAI one) have
no live fetch here. Rather than ship a static list of model names that could
be stale or simply wrong by the time someone reads it, those providers raise
`ModelFetchNotSupported` and the caller falls back to a free-text model field
— which every provider already supports regardless.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from groq import AsyncGroq
from openai import AsyncOpenAI
from pydantic_ai.providers import infer_provider_class

from basivo_orch.credentials.provider_client import construct_provider

#: Providers with no practical live catalog here. See the module docstring.
NO_LIVE_CATALOG = frozenset({"bedrock", "huggingface", "azure", "mistral", "cohere"})


class ModelFetchNotSupported(Exception):
    """This provider has no live catalog; the caller should offer free text."""


class ModelFetchFailed(Exception):
    """The provider rejected the request — almost always a bad key or URL."""


async def fetch_models(
    provider_name: str, *, api_key: str, base_url: str, options: dict[str, object]
) -> list[str]:
    if provider_name in NO_LIVE_CATALOG:
        raise ModelFetchNotSupported(provider_name)

    if provider_name == "google":
        return await _fetch_google(api_key=api_key, base_url=base_url, options=options)

    provider_cls = infer_provider_class(provider_name)
    provider = construct_provider(provider_cls, api_key=api_key, base_url=base_url, options=options)
    client = provider.client

    try:
        if isinstance(client, (AsyncOpenAI, AsyncAnthropic)):
            # Both `.models.list()` calls return an object that resolves to
            # the first page on `await` — one page is plenty for a dropdown,
            # and avoids paginating through every model a large provider has.
            page = await client.models.list()
            return sorted({model.id for model in page.data if getattr(model, "id", None)})

        if isinstance(client, AsyncGroq):
            response = await client.models.list()
            return sorted({model.id for model in response.data if getattr(model, "id", None)})
    except Exception as exc:  # noqa: BLE001 - surfaced as "the key didn't work"
        raise ModelFetchFailed(str(exc)) from exc

    raise ModelFetchNotSupported(provider_name)


async def _fetch_google(*, api_key: str, base_url: str, options: dict[str, object]) -> list[str]:
    provider_cls = infer_provider_class("google")
    provider = construct_provider(provider_cls, api_key=api_key, base_url=base_url, options=options)
    client = provider.client  # google.genai.Client

    try:
        pager = await client.aio.models.list()
        names = {
            name for model in pager.page if (name := (model.name or "").removeprefix("models/"))
        }
        return sorted(names)
    except Exception as exc:  # noqa: BLE001 - surfaced as "the key didn't work"
        raise ModelFetchFailed(str(exc)) from exc
