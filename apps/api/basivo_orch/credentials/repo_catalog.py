"""Which repositories a GitHub or GitLab credential can reach.

The builder's Repo field used to be a text box with "owner/name" as its hint,
and people typed the name without the owner. A list from the token itself is
both easier and correct by construction: whatever it shows, the token can
open.
"""

from __future__ import annotations

import httpx

DEFAULT_BASE = {"github": "https://api.github.com", "gitlab": "https://gitlab.com"}
#: Enough for a person to scroll; a token with more repos than this has a
#: search box in the picker.
PAGE_SIZE = 100


class RepoFetchNotSupported(Exception):
    """Not a code-hosting credential."""


class RepoFetchFailed(Exception):
    """The host said no; the message is theirs."""


async def fetch_repos(
    provider: str, *, api_key: str, base_url: str, http: httpx.AsyncClient | None = None
) -> list[str]:
    """Repositories the token can see, newest activity first, as owner/name."""
    if provider not in DEFAULT_BASE:
        raise RepoFetchNotSupported(provider)
    base = (base_url or DEFAULT_BASE[provider]).rstrip("/")
    owns = http is None
    client = http or httpx.AsyncClient(timeout=15.0)
    try:
        if provider == "github":
            response = await client.get(
                f"{base}/user/repos",
                params={
                    "per_page": PAGE_SIZE,
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/vnd.github+json",
                },
            )
            key = "full_name"
        else:
            response = await client.get(
                f"{base}/api/v4/projects",
                params={
                    "membership": "true",
                    "per_page": PAGE_SIZE,
                    "order_by": "last_activity_at",
                    "simple": "true",
                },
                headers={"PRIVATE-TOKEN": api_key},
            )
            key = "path_with_namespace"
    except httpx.HTTPError as exc:
        raise RepoFetchFailed(f"Could not reach {base}: {exc}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code >= 400:
        raise RepoFetchFailed(f"{response.status_code} {response.reason_phrase}".strip())
    try:
        items = response.json()
    except ValueError as exc:
        raise RepoFetchFailed("The host did not answer with JSON.") from exc
    if not isinstance(items, list):
        raise RepoFetchFailed("Unexpected answer from the host.")
    return [str(item[key]) for item in items if isinstance(item, dict) and item.get(key)]
