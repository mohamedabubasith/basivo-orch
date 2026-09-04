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


# ---------------------------------------------------------------------------
# Publishing connects the repository by itself
# ---------------------------------------------------------------------------

from basivo_orch.auth.authz import OrgContext, Permission, Role  # noqa: E402
from basivo_orch.auth.models import Organization, User  # noqa: E402
from basivo_orch.credentials.crypto import encrypt  # noqa: E402
from basivo_orch.credentials.models import Credential  # noqa: E402
from basivo_orch.flows import service  # noqa: E402
from basivo_orch.flows.graph import Graph  # noqa: E402
from basivo_orch.flows.router import publish_flow  # noqa: E402


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def listening_flow(session, organization, credential_id: str):
    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "hook",
                    "type": "trigger.webhook",
                    "name": "Webhook",
                    "config": {
                        "listen_provider": "github",
                        "listen_credential_id": credential_id,
                        "listen_repo": "acme/site",
                        "listen_events": ["issues"],
                    },
                },
                {
                    "id": "tidy",
                    "type": "data.set",
                    "name": "Tidy",
                    "config": {"assignments": [{"name": "x", "value": 1}]},
                },
            ],
            "edges": [{"source": "hook", "target": "tidy"}],
        }
    )
    flow, _ = await service.create_flow(
        session,
        organization_id=organization.id,
        user_id=None,
        name="Issue to PR",
        slug=None,
        description=None,
        graph=graph,
    )
    return flow


async def a_github_credential(session, organization) -> Credential:
    record = Credential(
        organization_id=organization.id,
        name="gh",
        provider="github",
        secret_encrypted=encrypt("ghp_saved"),
        base_url=None,
        options={},
    )
    session.add(record)
    await session.commit()
    return record


async def test_publishing_registers_the_webhook_the_trigger_asked_for(
    monkeypatch, session, organization
):
    """The person chose a repo on the trigger. Publish is the one action they
    take; the webhook appears at GitHub without a second step."""
    cred = await a_github_credential(session, organization)
    flow = await listening_flow(session, organization, str(cred.id))
    calls = []

    async def fake_register(http, *, token, repo, hook_url, secret, events, api_base):
        calls.append({"token": token, "repo": repo, "hook_url": hook_url, "events": events})
        return {"hook_id": 9, "events": events, "updated": False}

    monkeypatch.setattr("basivo_orch.flows.router.register_github_webhook", fake_register)
    result = await publish_flow(flow.id, context=make_context(organization), session=session)

    assert result["version"] == 1
    assert result["github"]["repo"] == "acme/site" and result["github"]["events"] == ["issues"]
    assert calls[0]["token"] == "ghp_saved" and calls[0]["hook_url"].endswith(f"/hooks/{flow.id}")
    assert calls[0]["repo"] == "acme/site"


async def test_a_github_refusal_does_not_unpublish_the_flow(monkeypatch, session, organization):
    cred = await a_github_credential(session, organization)
    flow = await listening_flow(session, organization, str(cred.id))

    async def refusing(http, **kwargs):
        raise HTTPException(400, "GitHub refused this credential for webhook administration.")

    monkeypatch.setattr("basivo_orch.flows.router.register_github_webhook", refusing)
    result = await publish_flow(flow.id, context=make_context(organization), session=session)

    assert result["version"] == 1, "the flow is fine; only the GitHub side failed"
    assert "refused" in result["github"]["error"]


async def test_a_plain_webhook_flow_publishes_without_touching_github(
    monkeypatch, session, organization
):
    cred = await a_github_credential(session, organization)
    flow = await listening_flow(session, organization, str(cred.id))
    # Same flow, but the trigger is not listening to anything.
    version = await service.latest_version(session, flow.id)
    graph = Graph.model_validate(version.graph)
    graph.nodes[0].config = {}
    await service.save_version(session, flow=flow, graph=graph, user_id=None)

    async def must_not_be_called(http, **kwargs):
        raise AssertionError("no repository was chosen")

    monkeypatch.setattr("basivo_orch.flows.router.register_github_webhook", must_not_be_called)
    result = await publish_flow(flow.id, context=make_context(organization), session=session)
    assert "github" not in result
