"""Searching the web, without a key and without a bill.

Every hosted search API worth using wants a key and a credit card: Brave,
Serper, Tavily, Exa. That is a fine trade for a company already paying for a
model, and a terrible first experience for somebody who has just drawn their
first flow — they wanted "look this up", not another signup.

So the default is DuckDuckGo, which needs neither. `ddgs` is a thin, maintained
client over the endpoint DuckDuckGo serves to browsers; it is unofficial, it is
rate limited, and it is free, which for "check what this company does before
writing the post" is exactly the right trade. A paid provider can be added
behind the same node later without the flow changing.

**Reading a page is not searching it.** A search returns titles, links and
snippets — that is often all a model needs, and it costs nothing. Fetching the
pages is opt-in, capped, and goes through the same SSRF guard the HTTP node
uses, because a URL chosen by a search engine from a query written by a model
is about as untrusted as a URL gets.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.http import assert_public_url
from basivo_orch.flows.templating import render_value

#: Per page fetched. Enough for an article, small enough that ten of them do
#: not become a model's whole context window.
MAX_PAGE_CHARS = 4_000
PAGE_TIMEOUT_SECONDS = 12.0
#: DuckDuckGo answers slowly under load and the whole node is worth abandoning
#: rather than holding a run open.
SEARCH_TIMEOUT_SECONDS = 25.0

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK = re.compile(r"\n{3,}")
_DROP = re.compile(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>")


class SearchResult(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""
    text: str = ""


async def search(query: str, *, count: int, region: str, safe: str) -> list[dict[str, str]]:
    """Titles, links and snippets for one query.

    Runs in a thread: `ddgs` is synchronous, and on the event loop it would
    stall the worker's heartbeat — which is how a run gets taken away from us
    mid-search by the reaper.
    """
    try:
        from ddgs import DDGS
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise NodeError(
            "Web search needs the `ddgs` package. It ships with the API image; "
            "install it locally with `uv sync`."
        ) from exc

    def run() -> list[dict[str, Any]]:
        with DDGS() as engine:
            return list(engine.text(query, region=region, safesearch=safe, max_results=count))

    try:
        found = await asyncio.wait_for(asyncio.to_thread(run), timeout=SEARCH_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise NodeError(
            f"The search did not answer within {int(SEARCH_TIMEOUT_SECONDS)} seconds. "
            "DuckDuckGo rate limits bursts; try again, or search less often."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        raise NodeError(f"The search failed: {type(exc).__name__}: {exc}") from exc

    return [
        {
            "title": str(item.get("title") or "").strip(),
            "url": str(item.get("href") or item.get("url") or "").strip(),
            "snippet": str(item.get("body") or "").strip(),
        }
        for item in found
        if item.get("href") or item.get("url")
    ]


def readable(html: str) -> str:
    """The words out of a page, roughly.

    Not a parser and not trying to be: scripts and styles go, tags go, runs of
    whitespace collapse. A model reading this gets the article; a model reading
    raw HTML gets a page of attributes and a bill.
    """
    text = _DROP.sub(" ", html)
    text = _TAG.sub("\n", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = _SPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return _BLANK.sub("\n\n", "\n".join(line for line in lines if line))


async def fetch_text(http: httpx.AsyncClient, url: str) -> str:
    """One page's words, or an empty string. Never raises: a page that will not
    load is one result of several, not a failed run."""
    try:
        assert_public_url(url)
        response = await http.get(
            url,
            timeout=PAGE_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BasivoBot/1.0)"},
        )
        if response.status_code >= 400:
            return ""
        if "html" not in response.headers.get("content-type", "") and not response.text:
            return ""
        return readable(response.text)[:MAX_PAGE_CHARS]
    except Exception:  # noqa: BLE001 - one bad page must not end the search
        return ""


class WebSearchConfig(BaseModel):
    model_config = {"extra": "forbid"}

    query: str = Field(
        min_length=1,
        max_length=400,
        title="What to search for",
        description="Supports {{ references }}, for example {{ input.text }}.",
    )
    count: int = Field(
        default=5,
        ge=1,
        le=15,
        title="How many results",
        description="Titles, links and snippets. Five is usually plenty.",
    )
    read_pages: int = Field(
        default=0,
        ge=0,
        le=5,
        title="Pages to read",
        description=(
            "Open this many of the top results and pull their text out. Costs a few seconds "
            "each. Leave at zero when the snippets are enough."
        ),
    )
    region: str = Field(
        default="wt-wt",
        max_length=12,
        title="Region",
        description="wt-wt is worldwide. in-en is India, uk-en the UK, us-en the US.",
    )
    safe: Literal["on", "moderate", "off"] = Field(default="moderate", title="Safe search")


class WebSearchNode(Node):
    """A query in, results out, optionally with the pages read."""

    type = "web.search"
    label = "Search the Web"
    description = "Search the web and hand the results to the next node. No key needed."
    when = (
        "The flow needs something it does not know: today's news, a company's own words, "
        "what a product costs. Put it before an agent or Generate with LLM and pass the "
        "results in."
    )
    needs = (
        "Nothing. No account and no key: it uses DuckDuckGo.",
        "A trigger before it, or any node whose output the query should come from.",
    )
    example = "Chat -> Search the Web -> Generate with LLM"
    tier = 1
    category = "data"
    config_model = WebSearchConfig
    output_paths = ("query", "count", "results", "text")

    max_attempts = 2
    retry_backoff_seconds = 3.0
    timeout_seconds = 120.0

    async def run(self, config: WebSearchConfig, ctx: NodeContext) -> NodeResult:
        template = ctx.template_context()
        query = str(render_value(config.query, template)).strip()
        if not query:
            raise NodeError(
                "The search query rendered empty. Check its reference against the output of "
                "the node before this one."
            )

        await ctx.progress(f"Searching for {query[:60]}")
        results = await search(
            query, count=config.count, region=config.region, safe=config.safe
        )
        await ctx.step("search.results", {"query": query, "count": len(results)})

        if config.read_pages and results:
            await ctx.progress(f"Reading {min(config.read_pages, len(results))} pages")
            pages = await asyncio.gather(
                *(
                    fetch_text(ctx.http, item["url"])
                    for item in results[: config.read_pages]
                )
            )
            for item, page in zip(results, pages, strict=False):
                item["text"] = page
            await ctx.step(
                "search.read",
                {"pages": sum(1 for page in pages if page), "asked_for": len(pages)},
            )

        if not results:
            raise NodeError(
                f"Nothing came back for {query!r}. DuckDuckGo rate limits bursts, so a flow "
                "searching in a loop will see this; otherwise try a plainer query."
            )

        # One block of text as well as the list, because the common next step is
        # "put this in a prompt" and nobody should have to write a loop for it.
        joined = "\n\n".join(
            f"{item['title']}\n{item['url']}\n{item.get('text') or item['snippet']}"
            for item in results
        )
        return NodeResult(
            output={
                "query": query,
                "count": len(results),
                "results": results,
                "text": joined[:20_000],
            }
        )
