"""Where search results come from, behind one interface.

The provider is an implementation detail and it will change. DuckDuckGo is
free and needs no key, which is why it is the default; it is also unofficial
and rate limited, so any deployment that leans on search will eventually want
a SearXNG instance of its own or a paid API. None of that should reach a flow,
a node's configuration, or a person watching a chat window: they asked for the
web to be searched, not for a particular company to search it.

So: one `SearchProvider` protocol, one registry, one setting. Adding Brave or
Tavily later is a class and a line in `PROVIDERS`, and every existing flow
picks it up without an edit.

Two behaviours worth stating, because both are about not lying to the caller:

**Falling back is silent but recorded.** When the first provider returns
nothing — rate limited, blocked, having a bad day — the next one is tried. The
run log says which one answered; the answer does not.

**News is a different question from the web.** "What happened today" against a
plain web index returns the homepage of a newspaper, which is how an agent ends
up inventing yesterday's scores from a page that said nothing. Providers expose
recency as a separate mode, and the agent's tool can ask for it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal, Protocol

from basivo_orch.flows.nodes.base import NodeError
from basivo_orch.logging import get_logger

log = get_logger(__name__)

Kind = Literal["web", "news"]

#: Long enough for a slow provider, short enough that a flow does not hang on
#: one. The node's own timeout is the real ceiling.
TIMEOUT_SECONDS = 20.0


class SearchProvider(Protocol):
    """What every search backend must be able to do."""

    #: Recorded on the run so an operator can see who answered. Never shown to
    #: a visitor.
    name: str

    async def search(
        self, query: str, *, count: int, kind: Kind, region: str
    ) -> list[dict[str, str]]:
        """Results as [{title, url, snippet, published}], newest first for news."""
        ...


class DuckDuckGo:
    """No key, no account, rate limited. The reason search works out of the box."""

    name = "duckduckgo"

    async def search(
        self, query: str, *, count: int, kind: Kind, region: str
    ) -> list[dict[str, str]]:
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise NodeError(
                "Web search needs the `ddgs` package. It ships with the API image; "
                "install it locally with `uv sync`."
            ) from exc

        def run() -> list[dict[str, Any]]:
            with DDGS() as engine:
                if kind == "news":
                    return list(engine.news(query, region=region, max_results=count))
                return list(engine.text(query, region=region, max_results=count))

        found = await asyncio.wait_for(asyncio.to_thread(run), timeout=TIMEOUT_SECONDS)
        return [
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or item.get("href") or "").strip(),
                "snippet": str(item.get("body") or item.get("excerpt") or "").strip(),
                "published": str(item.get("date") or "").strip(),
                "source": str(item.get("source") or "").strip(),
            }
            for item in found
            if item.get("url") or item.get("href")
        ]


class SearxNG:
    """A metasearch instance, usually your own.

    The answer to DuckDuckGo rate limiting a busy deployment: run SearXNG
    beside the worker, point `BASIVO_SEARXNG_URL` at it, and searches stop
    depending on somebody else's tolerance. It speaks JSON and needs no key.
    """

    name = "searxng"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def search(
        self, query: str, *, count: int, kind: Kind, region: str
    ) -> list[dict[str, str]]:
        import httpx

        params = {
            "q": query,
            "format": "json",
            "safesearch": "1",
            "categories": "news" if kind == "news" else "general",
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.get(f"{self.base_url}/search", params=params)
        if response.status_code >= 400:
            raise NodeError(f"The search instance answered {response.status_code}.")
        results = response.json().get("results", [])
        return [
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(item.get("content") or "").strip(),
                "published": str(item.get("publishedDate") or "").strip(),
                "source": str(item.get("engine") or "").strip(),
            }
            for item in results[:count]
            if item.get("url")
        ]


def configured() -> list[SearchProvider]:
    """The providers to try, in order.

    `BASIVO_SEARCH_PROVIDER` names the first choice; anything else configured
    follows it as a fallback, because a search that returns nothing is worse
    than a search that took an extra second.
    """
    searx_url = os.environ.get("BASIVO_SEARXNG_URL", "").strip()
    available: dict[str, SearchProvider] = {"duckduckgo": DuckDuckGo()}
    if searx_url:
        available["searxng"] = SearxNG(searx_url)

    preferred = os.environ.get("BASIVO_SEARCH_PROVIDER", "").strip().lower()
    order = [name for name in ([preferred] if preferred in available else []) if name]
    order += [name for name in available if name not in order]
    return [available[name] for name in order]


async def search(
    query: str, *, count: int = 5, kind: Kind = "web", region: str = "wt-wt"
) -> list[dict[str, str]]:
    """Search, through whichever provider answers.

    Raises only when every provider failed, and then with the last thing that
    went wrong rather than a generic apology.
    """
    providers = configured()
    last: Exception | None = None

    for provider in providers:
        try:
            results = await provider.search(query, count=count, kind=kind, region=region)
        except TimeoutError as exc:
            last = exc
            log.warning("search.timeout", provider=provider.name, query=query[:80])
            continue
        except Exception as exc:  # noqa: BLE001 - one provider failing is not the end
            last = exc
            log.warning("search.failed", provider=provider.name, error=str(exc)[:200])
            continue
        if results:
            log.info("search.answered", provider=provider.name, results=len(results))
            return results
        log.info("search.empty", provider=provider.name, query=query[:80])

    if last is not None:
        raise NodeError(
            f"The search could not be completed: {type(last).__name__}. "
            "Searching in a loop hits rate limits; try again in a moment."
        )
    return []
