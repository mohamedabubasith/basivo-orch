"""Searching the web: what comes back, and what a page turns into.

The network is never touched here. `search()` is exercised against a fake
client because a test that depends on DuckDuckGo answering is a test that
fails on a train, and the part worth pinning is our handling rather than
theirs.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.search import (
    MAX_PAGE_CHARS,
    WebSearchConfig,
    WebSearchNode,
    fetch_text,
    readable,
)


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass


def make_context(recorder: _Recorder, http: httpx.AsyncClient, text: str = "otters") -> NodeContext:
    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="search_1",
        node_name="Search the Web",
        attempt=1,
        input={"text": text},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=None,  # type: ignore[arg-type]
        http=http,
    )


def test_a_page_becomes_its_words() -> None:
    """A model reading raw HTML pays for attributes and closing tags."""
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert('x')</script></head>"
        "<body><h1>Otters</h1><p>They hold&nbsp;hands.</p></body></html>"
    )
    text = readable(html)
    assert "Otters" in text and "They hold hands." in text
    assert "alert" not in text and "color:red" not in text and "<" not in text


async def test_a_page_that_will_not_load_is_empty_rather_than_fatal() -> None:
    """One dead link among five results must not end the search."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_text(client, "https://example.com/gone") == ""


async def test_a_page_is_capped_before_it_reaches_a_prompt() -> None:
    body = "<html><body>" + ("otters hold hands. " * 2000) + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await fetch_text(client, "https://example.com/long")) <= MAX_PAGE_CHARS


async def test_a_private_address_is_refused_even_when_a_search_returned_it() -> None:
    """The URL was chosen by a search engine from a query a model wrote, which
    is about as untrusted as a URL gets."""
    async with httpx.AsyncClient() as client:
        assert await fetch_text(client, "http://169.254.169.254/latest/meta-data/") == ""


async def test_results_arrive_as_a_list_and_as_one_block_of_text(monkeypatch) -> None:
    """Both shapes, because the next node is either a loop or a prompt."""

    async def fake_search(query, *, count, region, safe):
        assert query == "otters"
        return [
            {"title": "Otters", "url": "https://a.example", "snippet": "They hold hands."},
            {"title": "More otters", "url": "https://b.example", "snippet": "Still do."},
        ]

    monkeypatch.setattr("basivo_orch.flows.nodes.search.search", fake_search)
    recorder = _Recorder()

    async with httpx.AsyncClient() as client:
        result = await WebSearchNode().run(
            WebSearchConfig(query="{{ input.text }}"), make_context(recorder, client)
        )

    assert result.output["count"] == 2
    assert result.output["results"][0]["url"] == "https://a.example"
    assert "They hold hands." in result.output["text"]
    assert "https://b.example" in result.output["text"]


async def test_nothing_found_says_what_to_do_about_it(monkeypatch) -> None:
    async def empty(query, *, count, region, safe):
        return []

    monkeypatch.setattr("basivo_orch.flows.nodes.search.search", empty)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="rate limits"):
            await WebSearchNode().run(
                WebSearchConfig(query="{{ input.text }}"), make_context(_Recorder(), client)
            )


async def test_an_empty_query_is_refused_before_the_search(monkeypatch) -> None:
    called: list[str] = []

    async def spy(query, *, count, region, safe):
        called.append(query)
        return []

    monkeypatch.setattr("basivo_orch.flows.nodes.search.search", spy)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NodeError, match="rendered empty"):
            await WebSearchNode().run(
                WebSearchConfig(query="{{ input.text }}"),
                make_context(_Recorder(), client, text="   "),
            )
    assert called == []
