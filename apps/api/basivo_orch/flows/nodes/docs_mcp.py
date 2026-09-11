"""The web, served to a coding agent as two tools over stdio.

A coding agent writes the API it was trained on. Libraries move, and the gap
between a model's training and today is measured in releases, so an agent
working from memory produces code that looked right a year ago. The fix is to
let it read the current documentation.

It must not do that with its own web tools. Claude Code, Codex and OpenCode
each have their own, with their own fetching rules, their own idea of what a
page is, and no connection to the provider interface the rest of this product
searches through. Two of the three would be reaching the network in ways the
worker cannot see. So `WebFetch` and `WebSearch` stay denied, and search
arrives the same way every other capability does: as a tool we implement.

Run as `python -m basivo_orch.flows.nodes.docs_mcp`, one process per turn,
speaking stdio. No port, no token, no listener: the server dies with the agent
that spawned it, which is the cheapest possible isolation.

`read_page` goes through `assert_public_url`, so a link in a bug report cannot
turn the worker into a probe of whatever network it runs in. That guard is the
reason this is a tool and not a line in a prompt.
"""

from __future__ import annotations

import httpx
from mcp.server.mcpserver import MCPServer

from basivo_orch.flows.nodes import search as web
from basivo_orch.flows.nodes import search_providers

#: Enough for the agent to see which result is the real documentation, and
#: little enough that five of them do not fill its context.
SNIPPET_CHARS = 300

server = MCPServer(
    name="docs",
    instructions=(
        "Look up current documentation before using an API you are not certain about, "
        "and write code from what you read rather than from memory."
    ),
)


@server.tool()
async def search_the_web(query: str, recent: bool = False, count: int = 5) -> str:
    """Search the web for documentation, release notes or error messages.

    Use it before writing code against a library you are unsure of, and when an
    error message is not one you recognise. Set `recent` for anything that
    changes with time, such as a version number or a deprecation.
    """
    count = max(1, min(10, count))
    results = await search_providers.search(query, count=count, kind="news" if recent else "web")
    if not results:
        return "No results."
    lines = []
    for item in results:
        published = f" ({item['published']})" if item.get("published") else ""
        lines.append(
            f"{item['title']}{published}\n{item['url']}\n{item['snippet'][:SNIPPET_CHARS]}"
        )
    return "\n\n".join(lines)


@server.tool()
async def read_page(url: str) -> str:
    """Read one page as text. Use it on a search result worth reading properly."""
    async with httpx.AsyncClient() as http:
        text = await web.fetch_text(http, url)
    return text or "That page could not be read."


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
