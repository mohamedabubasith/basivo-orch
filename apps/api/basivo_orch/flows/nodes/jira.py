"""Jira Cloud, as a place tickets come from and a place reports go back to.

Jira is not a git host, so it is not a `RepoClient`. It is the third party in
the flow "a ticket is filed -> the code is changed -> a pull request is opened":
the trigger listens to it, and Fix Code and Open PR comments the PR link on the
ticket when it is done.

A Jira credential is the site URL plus `email:api-token` — Jira Cloud's own
API tokens only work as HTTP Basic auth with the account's email, and one
field holding both keeps the credential model unchanged.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

import httpx

from basivo_orch.flows.nodes.base import NodeContext, NodeError

#: Events the trigger can ask Jira for. Keys are Jira's own event names.
JIRA_HOOK_EVENTS: dict[str, str] = {
    "jira:issue_created": "A ticket is created",
    "jira:issue_updated": "A ticket is updated",
    "comment_created": "Someone comments on a ticket",
}


def split_credential(api_key: str) -> tuple[str, str]:
    """`email:token` -> (email, token), or a sentence about what was expected."""
    email, sep, token = api_key.partition(":")
    if not sep or "@" not in email or not token.strip():
        raise NodeError(
            "A Jira credential is written email:api-token, for example "
            "you@company.com:ATATT3xFf. That is the account's email and an API token from "
            "id.atlassian.com/manage-profile/security/api-tokens."
        )
    return email.strip(), token.strip()


def basic_auth_header(api_key: str) -> dict[str, str]:
    email, token = split_credential(api_key)
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}", "Accept": "application/json"}


def site_url(base_url: str | None) -> str:
    """The Jira site, normalised. A bare host is accepted; a path is trimmed."""
    raw = (base_url or "").strip()
    if not raw:
        raise NodeError(
            "A Jira credential needs the site URL as its base URL, for example "
            "https://your-team.atlassian.net."
        )
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise NodeError(f"{raw!r} is not a Jira site URL.")
    return f"{parsed.scheme}://{parsed.netloc}"


def adf_text(node: Any) -> str:
    """Plain text out of an Atlassian Document Format tree.

    Jira sends descriptions and comments as ADF on its v3 API and on some
    webhooks, as wiki text on others. The agent wants words either way, so a
    string passes through and a document is flattened: paragraphs and list
    items become lines, text nodes become text, everything else is descended
    into and otherwise ignored.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_text(child) for child in node)
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return str((node.get("attrs") or {}).get("text", ""))
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url", ""))
    inner = "".join(adf_text(child) for child in node.get("content", []) or [])
    if kind in {"paragraph", "heading", "blockquote", "codeBlock", "rule"}:
        return inner + "\n"
    if kind == "listItem":
        return "- " + inner.rstrip("\n") + "\n"
    if kind in {"bulletList", "orderedList", "taskList", "panel", "table", "tableRow"}:
        return inner + ("\n" if kind != "tableRow" else "")
    if kind == "tableCell" or kind == "tableHeader":
        return inner.rstrip("\n") + " | "
    return inner


def adf_paragraphs(text: str) -> dict[str, Any]:
    """Plain text as an ADF document — one paragraph per blank-line-separated
    block, so a multi-line report reads as it was written."""
    blocks = [b for b in text.split("\n\n") if b.strip()] or [text]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": block.strip()}]}
            for block in blocks
        ],
    }


def ticket_from_payload(body: Any) -> dict[str, Any] | None:
    """The ticket in a Jira webhook delivery, in the shape a flow wants.

    None when the body is not a Jira issue event, so the trigger can add this
    to its output only when it applies.
    """
    if not isinstance(body, dict):
        return None
    issue = body.get("issue")
    event = str(body.get("webhookEvent", ""))
    if not isinstance(issue, dict) or not (event.startswith("jira:") or event == "comment_created"):
        return None
    fields = issue.get("fields") or {}
    key = str(issue.get("key", ""))
    self_url = str(issue.get("self", ""))
    site = f"{urlparse(self_url).scheme}://{urlparse(self_url).netloc}" if self_url else ""
    comment = body.get("comment") or {}
    return {
        "source": "jira",
        "event": event,
        "key": key,
        "title": str(fields.get("summary", "")),
        "description": adf_text(fields.get("description")).strip(),
        "type": str((fields.get("issuetype") or {}).get("name", "")),
        "status": str((fields.get("status") or {}).get("name", "")),
        "priority": str((fields.get("priority") or {}).get("name", "")),
        "project": str((fields.get("project") or {}).get("key", "")),
        "labels": [str(label) for label in fields.get("labels") or []],
        "url": f"{site}/browse/{key}" if site and key else "",
        "comment": adf_text(comment.get("body")).strip() if isinstance(comment, dict) else "",
    }


class JiraClient:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str, api_key: str) -> None:
        self.http = http
        self.site = site_url(base_url)
        self._headers = basic_auth_header(api_key)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self.http.request(
            method, f"{self.site}{path}", headers=self._headers, **kwargs
        )
        if response.status_code >= 400:
            raise NodeError(f"Jira {method} {path} → {response.status_code}: {response.text[:300]}")
        return response

    async def myself(self) -> dict[str, Any]:
        """Who the credential is. The connection test."""
        return (await self._request("GET", "/rest/api/3/myself")).json()

    async def create_comment(self, key: str, text: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf_paragraphs(text)}
        )
        data = response.json()
        comment_id = str(data.get("id", ""))
        return {
            "id": comment_id,
            "url": f"{self.site}/browse/{key}"
            + (f"?focusedCommentId={comment_id}" if comment_id else ""),
        }


async def make_jira_client(ctx: NodeContext, credential_id: str) -> JiraClient:
    credential = await ctx.resolve_credential(credential_id) if credential_id else None
    if credential is None:
        raise NodeError(
            "Pick a Jira credential on this node to report back on the ticket, or turn off "
            "'Comment on the issue when done'."
        )
    if credential.provider != "jira":
        raise NodeError(f"This credential is for {credential.provider!r}, not Jira.")
    return JiraClient(ctx.http, base_url=credential.base_url or "", api_key=credential.api_key)
