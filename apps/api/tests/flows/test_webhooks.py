"""The inbound-hook edge — the checks that stand between GitHub and a run.

These are the pure functions behind `/hooks/{flow_id}`. The GitHub case is
exercised with a byte-real delivery: the signature in the test is computed
the way GitHub computes it (HMAC-SHA256 of the raw body, hex, `sha256=`
prefix), so if our verification drifts from their sending, this file is
where it breaks.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import HTTPException

from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig
from basivo_orch.flows.webhooks import (
    authenticate_hook,
    ensure_method_allowed,
    hook_idempotency_key,
    wrap_hook_payload,
)

SECRET = "hunter2-rotate-me"
BODY = b'{"action": "opened", "issue": {"number": 7, "title": "add() is wrong"}}'


def signed_config() -> WebhookTriggerConfig:
    return WebhookTriggerConfig(require_signature=True, secret=SECRET)


def github_signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# authenticate_hook
# ---------------------------------------------------------------------------


def test_a_valid_github_signature_is_admitted():
    authenticate_hook(
        signed_config(),
        raw_body=BODY,
        headers={"x-hub-signature-256": github_signature(BODY)},
    )


def test_a_github_signature_over_a_tampered_body_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        authenticate_hook(
            signed_config(),
            raw_body=BODY + b" ",  # one byte off — a tampered or re-encoded body
            headers={"x-hub-signature-256": github_signature(BODY)},
        )
    assert excinfo.value.status_code == 401


def test_a_github_signature_with_the_wrong_secret_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        authenticate_hook(
            signed_config(),
            raw_body=BODY,
            headers={"x-hub-signature-256": github_signature(BODY, secret="guessed")},
        )
    assert excinfo.value.status_code == 401


def test_gitlabs_token_header_is_admitted():
    authenticate_hook(signed_config(), raw_body=BODY, headers={"x-gitlab-token": SECRET})


def test_the_plain_secret_header_is_admitted():
    authenticate_hook(signed_config(), raw_body=BODY, headers={"x-webhook-secret": SECRET})


def test_no_credential_at_all_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        authenticate_hook(signed_config(), raw_body=BODY, headers={})
    assert excinfo.value.status_code == 401


def test_a_trigger_without_a_secret_cannot_be_hooked_at_all():
    # No secret means the URL itself would be the only credential, and URLs
    # end up in logs. The endpoint refuses to exist rather than run open.
    with pytest.raises(HTTPException) as excinfo:
        authenticate_hook(
            WebhookTriggerConfig(),
            raw_body=BODY,
            headers={"x-webhook-secret": "anything"},
        )
    assert excinfo.value.status_code == 403
    assert "Require signature" in excinfo.value.detail


# ---------------------------------------------------------------------------
# ensure_method_allowed
# ---------------------------------------------------------------------------


def test_the_triggers_method_chips_are_enforced():
    config = WebhookTriggerConfig(methods=["POST"])
    ensure_method_allowed(config, "POST")
    with pytest.raises(HTTPException) as excinfo:
        ensure_method_allowed(config, "GET")
    assert excinfo.value.status_code == 405


# ---------------------------------------------------------------------------
# wrap_hook_payload
# ---------------------------------------------------------------------------


def test_a_json_delivery_is_parsed_and_secrets_are_scrubbed():
    payload = wrap_hook_payload(
        method="POST",
        headers={
            "content-type": "application/json",
            "x-github-event": "issues",
            "x-github-delivery": "d-1",
            "x-hub-signature-256": "sha256=deadbeef",
            "authorization": "Bearer leaked",
            "cookie": "session=leaked",
        },
        query={"env": "prod"},
        raw_body=BODY,
    )

    # The event body and routing headers are there for templates to use…
    assert payload["body"]["issue"]["number"] == 7
    assert payload["headers"]["x-github-event"] == "issues"
    assert payload["query"] == {"env": "prod"}
    assert payload["method"] == "POST"
    # …and nothing that authenticates anyone is stored on the run.
    for header in ("x-hub-signature-256", "authorization", "cookie"):
        assert header not in payload["headers"]


def test_a_non_json_body_survives_as_text_for_debugging():
    payload = wrap_hook_payload(
        method="POST", headers={}, query={}, raw_body=b"plain=form&data=1"
    )
    assert payload["body"] == "plain=form&data=1"


def test_an_empty_body_is_none_not_an_error():
    payload = wrap_hook_payload(method="GET", headers={}, query={}, raw_body=b"")
    assert payload["body"] is None


# ---------------------------------------------------------------------------
# hook_idempotency_key
# ---------------------------------------------------------------------------


def test_delivery_uuids_become_idempotency_keys():
    assert hook_idempotency_key({"x-github-delivery": "abc"}) == "gh-abc"
    assert hook_idempotency_key({"x-gitlab-event-uuid": "def"}) == "gl-def"
    assert hook_idempotency_key({}) is None
