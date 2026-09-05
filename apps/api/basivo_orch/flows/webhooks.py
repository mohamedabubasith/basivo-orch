"""The inbound-hook edge: authenticate and package a raw webhook delivery.

The `/hooks/{flow_id}` endpoint exists because the senders that matter cannot
hold an API key. A GitHub repository webhook offers exactly one credential
slot — a shared secret it uses to HMAC-sign the body — and GitLab sends its
secret verbatim in `X-Gitlab-Token`. So on this endpoint the webhook trigger's
own secret *is* the authentication, which is also why the endpoint refuses
flows whose trigger has `require_signature` off: without the secret there
would be no authentication at all, only an unguessable URL, and URLs end up
in logs.

Everything here is a pure function over (config, request parts) so the checks
are unit-testable without a running app; the router stays a thin assembly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import HTTPException, status

from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig

#: Headers that must never land in the run's stored payload. The whole
#: delivery is shown verbatim on the run detail page — that is a feature for
#: every header except the ones that authenticate the caller.
_SCRUBBED_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "x-api-key",
        "x-webhook-secret",
        "x-gitlab-token",
        "x-hub-signature",
        "x-hub-signature-256",
        "x-telegram-bot-api-secret-token",
    }
)


def authenticate_hook(
    config: WebhookTriggerConfig, *, raw_body: bytes, headers: Mapping[str, str]
) -> None:
    """Admit the request or raise. Accepts any one of the three secret forms.

    - `X-Hub-Signature-256: sha256=<hmac>` — GitHub, an HMAC of the raw body.
    - `X-Gitlab-Token: <secret>` — GitLab, the secret verbatim.
    - `X-Webhook-Secret: <secret>` — anything else (curl, Sentry, your app).

    All comparisons are constant-time; a check that leaks its prefix through
    timing is a secret with a countdown.
    """
    secret = config.secret.strip()
    if not config.require_signature or not secret:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This hook URL only works when the flow's webhook trigger has "
            "'Require signature' on. Its secret is what authenticates callers "
            "here. Turn it on, set a secret, and republish.",
        )
    key = secret.encode()

    if signature := headers.get("x-hub-signature-256"):
        expected = "sha256=" + hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature.encode(), expected.encode()):
            return
    # Telegram sends the exact string given to setWebhook, on every update.
    # Same shape as GitLab's: a shared secret, verbatim, in a header.
    for header in ("x-gitlab-token", "x-webhook-secret", "x-telegram-bot-api-secret-token"):
        if presented := headers.get(header):
            if hmac.compare_digest(presented.encode(), key):
                return

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "The webhook secret did not match. Send it as X-Hub-Signature-256 "
        "(GitHub), X-Gitlab-Token (GitLab), or X-Webhook-Secret.",
    )


def ensure_method_allowed(config: WebhookTriggerConfig, method: str) -> None:
    """The trigger's Methods chips, enforced at the edge."""
    if method not in config.methods:
        raise HTTPException(
            status.HTTP_405_METHOD_NOT_ALLOWED,
            f"This webhook accepts {', '.join(config.methods)}. Not {method}.",
        )


def wrap_hook_payload(
    *,
    method: str,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    raw_body: bytes,
) -> dict[str, Any]:
    """Package a raw delivery into the shape the webhook trigger node emits.

    JSON bodies are parsed (that is what every webhook provider sends when
    asked); anything else is kept as text rather than dropped, so a
    misconfigured sender is still debuggable from the run detail page.
    """
    body: Any
    text = raw_body.decode("utf-8", errors="replace")
    try:
        body = json.loads(text) if text.strip() else None
    except ValueError:
        body = text

    return {
        "body": body,
        "headers": {k: v for k, v in headers.items() if k.lower() not in _SCRUBBED_HEADERS},
        "query": dict(query),
        "method": method,
    }


def hook_idempotency_key(headers: Mapping[str, str]) -> str | None:
    """Both hosts stamp every delivery with a UUID and reuse it on redelivery.

    Mapping it onto the run table's idempotency column means a retried or
    manually redelivered webhook reports the original run instead of starting
    a second fix for the same issue.
    """
    if delivery := headers.get("x-github-delivery"):
        return f"gh-{delivery}"
    if delivery := headers.get("x-gitlab-event-uuid"):
        return f"gl-{delivery}"
    return None


def telegram_idempotency_key(body: Any) -> str | None:
    """Telegram's guarantee is at-least-once, and it means it.

    An update is redelivered until the webhook answers 200 — so a slow reply,
    a deploy mid-request, or a 502 all produce the same update again. Without
    this, one photo becomes three photos in the session and one /generate
    becomes three renders of the same job, which on a two-core box is the whole
    afternoon.

    `update_id` is monotonic per bot and stable across redeliveries, which is
    exactly what the run table's idempotency column wants.
    """
    if isinstance(body, dict) and (update_id := body.get("update_id")) is not None:
        return f"tg-{update_id}"
    return None


def telegram_hook_secret(flow_id: uuid.UUID) -> str:
    """The secret Telegram sends back on every update for this flow.

    Derived, not stored. Telegram only ever repeats the string given to
    `setWebhook`, so both ends can compute it from something both ends already
    have — which means no column, no migration, no secret sitting in a flow's
    config where an exported graph would carry it. Rotating the deployment's
    SECRET_KEY rotates every bot's webhook secret, which is the correct
    blast radius for a key compromise.
    """
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"telegram-hook:{flow_id}".encode(), hashlib.sha256).hexdigest()


def github_hook_secret(flow_id: uuid.UUID) -> str:
    """The secret this platform gives GitHub when it registers a webhook.

    Same idea as the Telegram secret: derived from the deployment key and the
    flow id, so it is never typed, stored in a config, or carried around by
    an exported graph. GitHub signs every delivery with it, and the inbound
    hook recomputes it to check the signature.
    """
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"github-hook:{flow_id}".encode(), hashlib.sha256).hexdigest()


def jira_hook_secret(flow_id: uuid.UUID) -> str:
    """The token in the URL this platform registers at Jira.

    Jira Cloud's webhook API has no signing secret to give it, so the URL
    itself carries one, as `?key=…`. Derived like the others: never stored,
    never typed, rotated with the deployment key. The URL is only ever seen by
    Jira's webhook settings page, which the registering account administers.
    """
    key = get_auth_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"jira-hook:{flow_id}".encode(), hashlib.sha256).hexdigest()


def authenticate_inbound(
    flow_id: uuid.UUID,
    config: WebhookTriggerConfig,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    query: Mapping[str, str] | None = None,
) -> None:
    """Admit a delivery with the trigger's own secret, or with the one this
    platform registered at GitHub or Jira on the author's behalf.

    A person who pressed Connect never saw a secret and should not need one:
    a GitHub-signed delivery is checked against the derived secret whenever
    the trigger's typed secret does not admit it (or there is none), and a
    flow listening to Jira admits the URL token it registered there.
    """
    try:
        authenticate_hook(config, raw_body=raw_body, headers=headers)
        return
    except HTTPException as denied:
        if config.listen_provider == "jira" and query is not None:
            presented = str(query.get("key", ""))
            if presented and hmac.compare_digest(
                presented.encode(), jira_hook_secret(flow_id).encode()
            ):
                return
        if "x-hub-signature-256" not in {k.lower() for k in headers}:
            raise
        platform = WebhookTriggerConfig(
            require_signature=True, secret=github_hook_secret(flow_id), methods=config.methods
        )
        try:
            authenticate_hook(platform, raw_body=raw_body, headers=headers)
        except HTTPException:
            raise denied from None


GITHUB_HOOK_EVENTS = ("issues", "pull_request", "push", "issue_comment")


async def register_github_webhook(
    http: httpx.AsyncClient,
    *,
    token: str,
    repo: str,
    hook_url: str,
    secret: str,
    events: list[str],
    api_base: str = "https://api.github.com",
) -> dict[str, Any]:
    """Create, or update, the one webhook on `repo` that points at `hook_url`.

    Idempotent on the URL: pressing Connect twice, or after changing the
    events, edits the existing hook rather than stacking a second one that
    would start every run twice.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = api_base.rstrip("/")
    body = {
        "name": "web",
        "active": True,
        "events": events,
        "config": {"url": hook_url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }
    listing = await http.get(f"{base}/repos/{repo}/hooks", headers=headers)
    if listing.status_code == 404:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"GitHub could not find {repo}, or this credential cannot administer it. "
            "The token needs the repo scope (or Webhooks: read and write on a fine-grained token).",
        )
    if listing.status_code in (401, 403):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "GitHub refused this credential for webhook administration. "
            "It needs the repo scope, or Webhooks: read and write on a fine-grained token.",
        )
    if listing.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GitHub answered {listing.status_code}.")
    existing = next(
        (h for h in listing.json() if (h.get("config") or {}).get("url") == hook_url), None
    )
    if existing:
        response = await http.patch(
            f"{base}/repos/{repo}/hooks/{existing['id']}", headers=headers, json=body
        )
    else:
        response = await http.post(f"{base}/repos/{repo}/hooks", headers=headers, json=body)
    if response.status_code >= 400:
        detail = ""
        try:
            detail = str(response.json().get("message", ""))
        except ValueError:
            pass
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"GitHub would not save the webhook: {detail or response.status_code}. "
            "The URL has to be https and publicly reachable.",
        )
    data = response.json()
    return {
        "hook_id": data.get("id"),
        "events": data.get("events", events),
        "updated": bool(existing),
    }


async def register_jira_webhook(
    http: httpx.AsyncClient,
    *,
    site: str,
    api_key: str,
    hook_url: str,
    events: list[str],
    jql: str = "",
    name: str = "Basivo",
) -> dict[str, Any]:
    """Create, or update, the one webhook on the Jira site that points at `hook_url`.

    Jira Cloud's webhook registration API (`/rest/webhooks/1.0/webhook`) is
    the one a site administrator's API token can use. Idempotent on the URL,
    like the GitHub version: reconnecting edits the existing webhook rather
    than stacking another that would start every run twice.
    """
    from basivo_orch.flows.nodes.jira import basic_auth_header

    headers = {**basic_auth_header(api_key), "Content-Type": "application/json"}
    base = site.rstrip("/")
    body: dict[str, Any] = {
        "name": name,
        "url": hook_url,
        "events": events,
        "excludeBody": False,
    }
    if jql.strip():
        body["filters"] = {"issue-related-events-section": jql.strip()}

    listing = await http.get(f"{base}/rest/webhooks/1.0/webhook", headers=headers)
    if listing.status_code in (401, 403):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Jira refused this credential for webhook administration. The account has to be "
            "a Jira administrator on that site, and the credential is written email:api-token.",
        )
    if listing.status_code == 404:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No Jira site answered at {base}. The credential's base URL should be the site, "
            "for example https://your-team.atlassian.net.",
        )
    if listing.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Jira answered {listing.status_code}.")
    try:
        hooks = listing.json()
    except ValueError:
        hooks = []
    # The registered URL carries the flow's token; match on everything before
    # the query string so a rotated deployment key still finds its own hook.
    plain = hook_url.split("?", 1)[0]
    existing = next(
        (
            h
            for h in hooks
            if isinstance(h, dict) and str(h.get("url", "")).split("?", 1)[0] == plain
        ),
        None,
    )
    if existing:
        hook_id = str(existing.get("self", "")).rstrip("/").rsplit("/", 1)[-1]
        response = await http.put(
            f"{base}/rest/webhooks/1.0/webhook/{hook_id}", headers=headers, json=body
        )
    else:
        response = await http.post(f"{base}/rest/webhooks/1.0/webhook", headers=headers, json=body)
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = str(
                payload.get("message") or "; ".join(payload.get("messages", []) or []) or ""
            )
        except ValueError:
            pass
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Jira would not save the webhook: {detail or response.status_code}. "
            "The URL has to be https and publicly reachable.",
        )
    data = response.json() if response.content else {}
    return {
        "hook_id": str(data.get("self", "")).rstrip("/").rsplit("/", 1)[-1] or None,
        "events": list(data.get("events", events) or events),
        "updated": bool(existing),
    }
