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
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, status

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
            "'Require signature' on — its secret is what authenticates callers "
            "here. Turn it on, set a secret, and republish.",
        )
    key = secret.encode()

    if signature := headers.get("x-hub-signature-256"):
        expected = "sha256=" + hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature.encode(), expected.encode()):
            return
    for header in ("x-gitlab-token", "x-webhook-secret"):
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
            f"This webhook accepts {', '.join(config.methods)} — not {method}.",
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
