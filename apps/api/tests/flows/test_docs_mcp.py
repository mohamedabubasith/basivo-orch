"""The documentation server the coding agents get.

Two tools, and the only interesting thing about either is what they refuse to
do: `read_page` goes through the same public URL guard as the search node, so
a link in a bug report cannot turn a repair into a probe of the network the
worker sits in.
"""

from __future__ import annotations

import pytest

from basivo_orch.flows.nodes import docs_mcp

pytestmark = pytest.mark.anyio


async def test_a_search_comes_back_as_lines_an_agent_can_read(monkeypatch):
    async def fake_search(query, *, count=5, kind="web", region="wt-wt"):
        assert kind == "news" and count == 3
        return [
            {
                "title": "Vite 7 released",
                "url": "https://vite.dev/blog/announcing-vite7",
                "snippet": "The build tool ships rolldown.",
                "published": "2026-04-01",
                "source": "vite.dev",
            }
        ]

    monkeypatch.setattr(docs_mcp.search_providers, "search", fake_search)
    result = await docs_mcp.server.call_tool(
        "search_the_web", {"query": "vite 7", "recent": True, "count": 3}
    )
    text = str(result)
    assert "Vite 7 released" in text and "https://vite.dev/blog/announcing-vite7" in text
    # The supplier never reaches the agent, so a change of provider is not a
    # change of behaviour anyone has written a prompt against.
    assert "duckduckgo" not in text.lower() and "searxng" not in text.lower()


async def test_nothing_found_says_so_rather_than_inventing(monkeypatch):
    async def nothing(query, *, count=5, kind="web", region="wt-wt"):
        return []

    monkeypatch.setattr(docs_mcp.search_providers, "search", nothing)
    result = await docs_mcp.server.call_tool("search_the_web", {"query": "nothing at all"})
    assert "No results." in str(result)


async def test_a_private_address_is_not_fetched():
    """The guard is the reason this is a tool of ours rather than the CLI's."""
    result = await docs_mcp.server.call_tool(
        "read_page", {"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert "could not be read" in str(result)
