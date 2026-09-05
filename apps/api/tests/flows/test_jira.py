"""Jira as a ticket source: the trigger, the webhook registration, and the
report back on the ticket. Every HTTP exchange is against a scripted Jira."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from basivo_orch.flows.nodes.base import NodeContext, NodeError, ResolvedCredential
from basivo_orch.flows.nodes.jira import (
    JiraClient,
    adf_text,
    site_url,
    split_credential,
    ticket_from_payload,
)
from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig, WebhookTriggerNode
from basivo_orch.flows.webhooks import (
    authenticate_inbound,
    jira_hook_secret,
    register_jira_webhook,
)

FLOW = uuid.UUID("22222222-2222-2222-2222-222222222222")
HOOK = f"https://console.example/hooks/{FLOW}"

ADF = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Remove all the code"},
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"id": "1", "text": "@abu"}},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                    ],
                },
            ],
        },
    ],
}


def jira_payload(description=ADF, event="jira:issue_created") -> dict:
    return {
        "timestamp": 1,
        "webhookEvent": event,
        "issue": {
            "id": "10001",
            "key": "OPS-7",
            "self": "https://acme.atlassian.net/rest/api/2/issue/10001",
            "fields": {
                "summary": "Rewrite the pricing module",
                "description": description,
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": {"name": "High"},
                "project": {"key": "OPS"},
                "labels": ["autofix"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Reading a ticket
# ---------------------------------------------------------------------------


def test_adf_becomes_plain_text():
    assert adf_text(ADF) == "Remove all the code\n@abu\n- one\n- two\n\n"
    assert adf_text("already text") == "already text"
    assert adf_text(None) == ""


def test_a_jira_delivery_is_normalised_into_a_ticket():
    ticket = ticket_from_payload(jira_payload())
    assert ticket is not None
    assert ticket["key"] == "OPS-7"
    assert ticket["title"] == "Rewrite the pricing module"
    assert ticket["description"].startswith("Remove all the code")
    assert ticket["url"] == "https://acme.atlassian.net/browse/OPS-7"
    assert (
        ticket["project"] == "OPS" and ticket["type"] == "Task" and ticket["labels"] == ["autofix"]
    )


def test_a_github_delivery_is_not_mistaken_for_a_ticket():
    assert ticket_from_payload({"action": "opened", "issue": {"number": 4}}) is None
    assert ticket_from_payload("not json") is None


async def test_the_webhook_trigger_offers_the_ticket_flattened():
    async def noop(*args, **kwargs):
        return None

    ctx = NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="hook",
        node_name="Webhook",
        attempt=1,
        input=None,
        outputs={},
        variables={},
        trigger={"payload": {"body": jira_payload(), "headers": {}, "query": {}, "method": "POST"}},
        progress=noop,
        step=noop,
        resolve_credential=noop,
        http=httpx.AsyncClient(),
    )
    result = await WebhookTriggerNode().run(WebhookTriggerConfig(), ctx)
    assert result.output["ticket"]["key"] == "OPS-7"
    assert result.output["body"]["issue"]["key"] == "OPS-7", "the raw body is still there"

    ctx.trigger = {"payload": {"body": {"action": "opened"}, "headers": {}, "query": {}}}
    result = await WebhookTriggerNode().run(WebhookTriggerConfig(), ctx)
    assert "ticket" not in result.output


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


def test_the_credential_is_email_and_token_in_one_field():
    assert split_credential("me@acme.com:ATATT3x") == ("me@acme.com", "ATATT3x")
    with pytest.raises(NodeError, match="email:api-token"):
        split_credential("ATATT3x-only")


def test_the_site_url_is_normalised():
    assert site_url("acme.atlassian.net") == "https://acme.atlassian.net"
    assert site_url("https://acme.atlassian.net/jira/software") == "https://acme.atlassian.net"
    with pytest.raises(NodeError, match="site URL"):
        site_url("")


# ---------------------------------------------------------------------------
# Registering the webhook
# ---------------------------------------------------------------------------


def jira_site(requests: list[httpx.Request], existing: list[dict] | None = None, *, admin=True):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host != "acme.atlassian.net":
            return httpx.Response(404, text="no such site")
        if not admin:
            return httpx.Response(403, json={"message": "Forbidden"})
        if request.method == "GET" and request.url.path == "/rest/webhooks/1.0/webhook":
            return httpx.Response(200, json=existing or [])
        if request.method == "POST" and request.url.path == "/rest/webhooks/1.0/webhook":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "self": "https://acme.atlassian.net/rest/webhooks/1.0/webhook/31",
                    "events": body["events"],
                },
            )
        if request.method == "PUT" and request.url.path == "/rest/webhooks/1.0/webhook/9":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "self": "https://acme.atlassian.net/rest/webhooks/1.0/webhook/9",
                    "events": body["events"],
                },
            )
        return httpx.Response(500, json={"message": f"unexpected {request.method} {request.url}"})

    return handler


async def test_connect_registers_the_url_with_its_token_and_filter():
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(jira_site(requests))) as http:
        result = await register_jira_webhook(
            http,
            site="https://acme.atlassian.net",
            api_key="me@acme.com:tok",
            hook_url=HOOK + "?key=abc",
            events=["jira:issue_created"],
            jql="project = OPS",
            name="Basivo flow Issue to PR",
        )
    assert result == {"hook_id": "31", "events": ["jira:issue_created"], "updated": False}
    post = next(r for r in requests if r.method == "POST")
    body = json.loads(post.content)
    assert body["url"] == HOOK + "?key=abc"
    assert body["filters"] == {"issue-related-events-section": "project = OPS"}
    assert body["excludeBody"] is False
    assert post.headers["authorization"].startswith("Basic ")


async def test_connecting_twice_updates_the_existing_webhook():
    requests: list[httpx.Request] = []
    existing = [
        {
            "self": "https://acme.atlassian.net/rest/webhooks/1.0/webhook/9",
            "url": HOOK + "?key=old-token",
            "events": ["jira:issue_created"],
        }
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(jira_site(requests, existing))
    ) as http:
        result = await register_jira_webhook(
            http,
            site="https://acme.atlassian.net",
            api_key="me@acme.com:tok",
            hook_url=HOOK + "?key=new-token",
            events=["jira:issue_created", "jira:issue_updated"],
        )
    assert result["updated"] is True and result["hook_id"] == "9"
    assert [r.method for r in requests] == ["GET", "PUT"]


async def test_a_non_admin_credential_is_a_plain_sentence():
    async with httpx.AsyncClient(transport=httpx.MockTransport(jira_site([], admin=False))) as http:
        with pytest.raises(HTTPException) as denied:
            await register_jira_webhook(
                http,
                site="https://acme.atlassian.net",
                api_key="me@acme.com:tok",
                hook_url=HOOK,
                events=["jira:issue_created"],
            )
    assert "Jira administrator" in denied.value.detail


async def test_a_wrong_site_is_a_plain_sentence():
    async with httpx.AsyncClient(transport=httpx.MockTransport(jira_site([]))) as http:
        with pytest.raises(HTTPException) as denied:
            await register_jira_webhook(
                http,
                site="https://nowhere.atlassian.net",
                api_key="me@acme.com:tok",
                hook_url=HOOK,
                events=["jira:issue_created"],
            )
    assert "No Jira site answered" in denied.value.detail


# ---------------------------------------------------------------------------
# Admitting the delivery
# ---------------------------------------------------------------------------


def test_a_jira_flow_admits_the_token_it_registered():
    config = WebhookTriggerConfig(listen_provider="jira", listen_credential_id="c1")
    authenticate_inbound(
        FLOW, config, raw_body=b"{}", headers={}, query={"key": jira_hook_secret(FLOW)}
    )


def test_a_wrong_token_is_refused():
    config = WebhookTriggerConfig(listen_provider="jira", listen_credential_id="c1")
    with pytest.raises(HTTPException):
        authenticate_inbound(FLOW, config, raw_body=b"{}", headers={}, query={"key": "nope"})
    with pytest.raises(HTTPException):
        authenticate_inbound(FLOW, config, raw_body=b"{}", headers={}, query={})


def test_a_flow_not_listening_to_jira_ignores_the_token():
    """The URL token is a Jira accommodation, not a general back door."""
    config = WebhookTriggerConfig()
    with pytest.raises(HTTPException):
        authenticate_inbound(
            FLOW, config, raw_body=b"{}", headers={}, query={"key": jira_hook_secret(FLOW)}
        )


def test_the_token_is_per_flow():
    assert jira_hook_secret(FLOW) != jira_hook_secret(uuid.uuid4())


# ---------------------------------------------------------------------------
# Reporting back
# ---------------------------------------------------------------------------


async def test_the_comment_is_posted_as_a_document_and_linked():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/rest/api/3/issue/OPS-7/comment"
        return httpx.Response(201, json={"id": "501"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = JiraClient(http, base_url="acme.atlassian.net", api_key="me@acme.com:tok")
        comment = await client.create_comment("OPS-7", "Opened a PR.\n\nIt rewrites pricing.")
    assert comment == {
        "id": "501",
        "url": "https://acme.atlassian.net/browse/OPS-7?focusedCommentId=501",
    }
    body = json.loads(requests[0].content)["body"]
    assert body["type"] == "doc"
    assert [p["content"][0]["text"] for p in body["content"]] == [
        "Opened a PR.",
        "It rewrites pricing.",
    ]


# ---------------------------------------------------------------------------
# Publishing connects the site by itself
# ---------------------------------------------------------------------------

from basivo_orch.auth.authz import OrgContext, Permission, Role  # noqa: E402
from basivo_orch.auth.models import Organization, User  # noqa: E402
from basivo_orch.credentials.crypto import encrypt  # noqa: E402
from basivo_orch.credentials.models import Credential  # noqa: E402
from basivo_orch.flows import service  # noqa: E402
from basivo_orch.flows.graph import Graph  # noqa: E402
from basivo_orch.flows.router import connect_jira_site, publish_flow  # noqa: E402
from basivo_orch.flows.schemas import JiraConnect  # noqa: E402


def make_context(organization: Organization) -> OrgContext:
    user = User(id=uuid.uuid4(), email="owner@example.com", hashed_password="x", is_active=True)  # noqa: S106
    return OrgContext(
        user=user, organization=organization, role=Role.OWNER, permissions=frozenset(Permission)
    )


async def a_credential(session, organization, provider="jira") -> Credential:
    record = Credential(
        organization_id=organization.id,
        name=provider,
        provider=provider,
        secret_encrypted=encrypt("me@acme.com:tok"),
        base_url="https://acme.atlassian.net" if provider == "jira" else None,
        options={},
    )
    session.add(record)
    await session.commit()
    return record


async def jira_flow(session, organization, credential_id: str):
    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "hook",
                    "type": "trigger.webhook",
                    "name": "Webhook",
                    "config": {
                        "listen_provider": "jira",
                        "listen_credential_id": credential_id,
                        "listen_filter": "project = OPS",
                        "listen_events": ["jira:issue_created"],
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
        name="Ticket to PR",
        slug=None,
        description=None,
        graph=graph,
    )
    return flow


async def test_publishing_registers_the_jira_webhook(monkeypatch, session, organization):
    cred = await a_credential(session, organization)
    flow = await jira_flow(session, organization, str(cred.id))
    calls = []

    async def fake_register(http, *, site, api_key, hook_url, events, jql, name):
        calls.append({"site": site, "api_key": api_key, "hook_url": hook_url, "jql": jql})
        return {"hook_id": "31", "events": events, "updated": False}

    monkeypatch.setattr("basivo_orch.flows.router.register_jira_webhook", fake_register)
    result = await publish_flow(flow.id, context=make_context(organization), session=session)

    assert result["version"] == 1
    assert result["jira"]["site"] == "https://acme.atlassian.net"
    assert result["jira"]["filter"] == "project = OPS"
    assert calls[0]["api_key"] == "me@acme.com:tok"
    assert calls[0]["hook_url"] == (
        f"{result['jira']['webhook']}?key={jira_hook_secret(flow.id)}"
    ), "the registered URL carries the token; the shown one does not"


async def test_connecting_with_a_github_credential_is_refused(session, organization):
    cred = await a_credential(session, organization, provider="github")
    flow = await jira_flow(session, organization, str(cred.id))
    with pytest.raises(HTTPException) as denied:
        await connect_jira_site(
            flow.id,
            JiraConnect(credential_id=str(cred.id)),
            context=make_context(organization),
            session=session,
        )
    assert denied.value.detail == "Pick a Jira credential first."
