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
