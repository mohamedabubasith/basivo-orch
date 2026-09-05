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

import httpx

from basivo_orch.flows.nodes.models import OPENAI_COMPATIBLE

#: Providers with no practical live catalog here. See the module docstring.
NO_LIVE_CATALOG = frozenset({"bedrock", "huggingface", "azure", "mistral", "cohere"})


class ModelFetchNotSupported(Exception):
    """This provider has no live catalog; the caller should offer free text."""


class ModelFetchFailed(Exception):
    """The provider rejected the request — almost always a bad key or URL."""


async def fetch_models(
    provider_name: str, *, api_key: str, base_url: str, options: dict[str, object]
) -> list[str]:
    if provider_name in ("telegram", "mastodon", "bluesky", "discord", "slack"):
        # These have no model catalogue; the same call proves the credential
        # works, which is the question the UI is really asking.
        return await _verify_posting_token(
            provider_name, api_key=api_key, base_url=base_url, options=options
        )

    if provider_name in ("github", "gitlab"):
        # VCS credentials have no model catalog; their "test" is the identity
        # endpoint — proves the token works without touching a repository.
        return await _verify_vcs_token(provider_name, api_key=api_key, base_url=base_url)

    if provider_name == "jira":
        return await _verify_jira(api_key=api_key, base_url=base_url)

    if provider_name in NO_LIVE_CATALOG:
        raise ModelFetchNotSupported(provider_name)

    if provider_name == "google":
        return await _fetch_google(api_key=api_key, base_url=base_url)

    if provider_name == "anthropic":
        if api_key.startswith("sk-ant-oat"):
            # A `claude setup-token` subscription token. It signs Claude Code in
            # and nothing else: the Messages API, and this catalog, want a real
            # API key. Not an error; there is simply no list to fetch.
            raise ModelFetchNotSupported("anthropic subscription token")
        return await _fetch_anthropic(api_key=api_key, base_url=base_url)

    return await _fetch_openai_compatible(provider_name, api_key=api_key, base_url=base_url)


async def _get(url: str, headers: dict[str, str]) -> dict:
    """One GET, with the provider's own error text preserved.

    A catalogue fetch doubles as the connection test, so "why did my key not
    work" must survive to the UI — a bare status code sends the user to the
    provider's documentation blind.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ModelFetchFailed(str(exc)) from exc
    if response.status_code >= 400:
        raise ModelFetchFailed(f"{response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ModelFetchFailed(f"{url} did not answer with JSON") from exc


async def _fetch_openai_compatible(provider_name: str, *, api_key: str, base_url: str) -> list[str]:
    """`GET {base}/models` — the one endpoint every OpenAI-compatible host has.

    This is why a new model needs no code change anywhere: the list comes from
    the provider at the moment the user opens the dropdown.
    """
    endpoint = base_url or OPENAI_COMPATIBLE.get(provider_name) or "https://api.openai.com/v1"
    payload = await _get(endpoint.rstrip("/") + "/models", {"Authorization": f"Bearer {api_key}"})
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ModelFetchNotSupported(provider_name)
    return sorted({m["id"] for m in data if isinstance(m, dict) and m.get("id")})


async def _fetch_anthropic(*, api_key: str, base_url: str) -> list[str]:
    payload = await _get(
        (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/models",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    data = payload.get("data", [])
    return sorted({m["id"] for m in data if isinstance(m, dict) and m.get("id")})


async def _fetch_google(*, api_key: str, base_url: str) -> list[str]:
    base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    payload = await _get(f"{base}/v1beta/models?key={api_key}", {})
    return sorted(
        {
            name
            for model in payload.get("models", [])
            if (name := (model.get("name") or "").removeprefix("models/"))
        }
    )


async def _verify_vcs_token(provider_name: str, *, api_key: str, base_url: str) -> list[str]:
    if provider_name == "github":
        url = (base_url or "https://api.github.com").rstrip("/") + "/user"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/vnd.github+json"}
    else:
        url = (base_url or "https://gitlab.com").rstrip("/") + "/api/v4/user"
        headers = {"PRIVATE-TOKEN": api_key}

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ModelFetchFailed(str(exc)) from exc
    if response.status_code >= 400:
        raise ModelFetchFailed(f"{response.status_code}: {response.text[:200]}")
    return []


async def _verify_posting_token(
    provider_name: str, *, api_key: str, base_url: str, options: dict[str, object]
) -> list[str]:
    """Prove a posting credential works, without posting anything.

    Every check here is read-only. A connection test that publishes a message
    to find out whether it can publish messages is not a test.
    """
    if provider_name == "telegram":
        base = (base_url or "https://api.telegram.org").rstrip("/")
        payload = await _get(f"{base}/bot{api_key}/getMe", {})
        bot = payload.get("result", {}) if isinstance(payload, dict) else {}
        if not bot.get("username"):
            raise ModelFetchFailed("Telegram did not recognise that bot token.")
        return [f"@{bot['username']}"]

    if provider_name == "mastodon":
        base = (base_url or "https://mastodon.social").rstrip("/")
        payload = await _get(
            f"{base}/api/v1/accounts/verify_credentials", {"Authorization": f"Bearer {api_key}"}
        )
        return [f"@{payload.get('username', 'account')}"]

    if provider_name == "bluesky":
        base = (base_url or "https://bsky.social").rstrip("/")
        handle = str((options or {}).get("identifier") or "").strip()
        if not handle:
            raise ModelFetchFailed(
                "Bluesky needs the account handle too. Add identifier to the credential's "
                "options, e.g. yourname.bsky.social."
            )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{base}/xrpc/com.atproto.server.createSession",
                    json={"identifier": handle, "password": api_key},
                )
        except httpx.HTTPError as exc:
            raise ModelFetchFailed(str(exc)) from exc
        if response.status_code >= 400:
            raise ModelFetchFailed(f"{response.status_code}: {response.text[:200]}")
        return [handle]

    # Discord and Slack webhooks carry their secret in the URL. Discord will
    # describe a webhook on GET; Slack will not, so its credential is accepted
    # on shape alone rather than by firing a test message into someone's
    # channel.
    webhook = (base_url or api_key).strip()
    if not webhook.startswith("https://"):
        raise ModelFetchFailed("That should be the full webhook URL, starting with https://.")
    if provider_name == "discord":
        payload = await _get(webhook, {})
        return [str(payload.get("name") or "webhook")]
    return ["webhook (accepted without a test message)"]


async def _verify_jira(*, api_key: str, base_url: str) -> list[str]:
    """Who the Jira credential is. Read-only; proves site, email and token."""
    import httpx

    from basivo_orch.flows.nodes.base import NodeError
    from basivo_orch.flows.nodes.jira import JiraClient

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            me = await JiraClient(client, base_url=base_url, api_key=api_key).myself()
    except NodeError as exc:
        raise ModelFetchFailed(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ModelFetchFailed(str(exc)) from exc
    return [str(me.get("displayName") or me.get("emailAddress") or "account")]
