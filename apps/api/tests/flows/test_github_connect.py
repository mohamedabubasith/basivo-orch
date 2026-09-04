"""Connect a GitHub repository to a flow without anyone touching GitHub's settings."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig
from basivo_orch.flows.webhooks import (
    authenticate_inbound,
    github_hook_secret,
    register_github_webhook,
)

HOOK = "https://console.example/hooks/11111111-1111-1111-1111-111111111111"


def github(requests: list[httpx.Request], existing: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/repos/acme/site/hooks":
            return httpx.Response(200, json=existing or [])
        if request.method == "POST" and request.url.path == "/repos/acme/site/hooks":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": 501, "events": body["events"]})
        if request.method == "PATCH" and request.url.path == "/repos/acme/site/hooks/77":
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": 77, "events": body["events"]})
        if request.url.path.startswith("/repos/nobody/"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {request.url.path}"}
        )

    return handler


async def test_connect_creates_the_webhook_with_url_secret_and_events():
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(github(seen))) as http:
        result = await register_github_webhook(
            http, token="ghp_x", repo="acme/site", hook_url=HOOK, secret="s3cret", events=["issues"]
        )
    assert result == {"hook_id": 501, "events": ["issues"], "updated": False}
    created = json.loads([r for r in seen if r.method == "POST"][0].content)
    assert created["config"] == {
        "url": HOOK,
        "content_type": "json",
        "secret": "s3cret",
        "insecure_ssl": "0",
    }
    assert created["active"] is True and created["events"] == ["issues"]
    assert seen[0].headers["authorization"] == "Bearer ghp_x"


async def test_connecting_twice_updates_the_same_hook_instead_of_adding_one():
    seen: list[httpx.Request] = []
    existing = [{"id": 77, "config": {"url": HOOK}}, {"id": 3, "config": {"url": "https://other"}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(github(seen, existing))) as http:
        result = await register_github_webhook(
            http, token="t", repo="acme/site", hook_url=HOOK, secret="s", events=["issues", "push"]
        )
    assert result["updated"] is True and result["hook_id"] == 77
    assert [r.method for r in seen] == ["GET", "PATCH"]


async def test_a_repo_the_token_cannot_administer_is_a_plain_sentence():
    async with httpx.AsyncClient(transport=httpx.MockTransport(github([]))) as http:
        with pytest.raises(HTTPException) as caught:
            await register_github_webhook(
                http, token="t", repo="nobody/private", hook_url=HOOK, secret="s", events=["issues"]
            )
    assert "could not find nobody/private" in caught.value.detail


def signed(secret: str, body: bytes) -> dict[str, str]:
    return {
        "x-hub-signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    }


def test_a_delivery_signed_with_the_platform_secret_is_admitted_without_a_typed_secret():
    """The person pressed Connect and never saw a secret. GitHub signs with the
    derived one; the inbound hook must accept exactly that."""
    flow_id = uuid.uuid4()
    body = b'{"action":"opened"}'
    config = WebhookTriggerConfig()  # no secret, signature not required
    authenticate_inbound(
        flow_id, config, raw_body=body, headers=signed(github_hook_secret(flow_id), body)
    )


def test_the_platform_secret_is_per_flow_and_never_the_same_twice():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert github_hook_secret(a) != github_hook_secret(b)
    assert len(github_hook_secret(a)) == 64


def test_a_wrong_signature_is_still_refused_when_no_typed_secret_exists():
    flow_id = uuid.uuid4()
    body = b"{}"
    with pytest.raises(HTTPException) as caught:
        authenticate_inbound(
            flow_id, WebhookTriggerConfig(), raw_body=body, headers=signed("guess", body)
        )
    assert caught.value.status_code in (401, 403)


def test_a_typed_secret_still_works_as_before():
    flow_id = uuid.uuid4()
    body = b"{}"
    config = WebhookTriggerConfig(require_signature=True, secret="typed")
    authenticate_inbound(flow_id, config, raw_body=body, headers=signed("typed", body))
    with pytest.raises(HTTPException):
        authenticate_inbound(flow_id, config, raw_body=body, headers={"x-webhook-secret": "nope"})
