"""The webhook email provider.

The endpoint on the other end receives password-reset and email-verification
links, which are credentials. These tests encode the properties that keep that
arrangement safe rather than merely checking that a request goes out.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from basivo_orch.auth.email import sender
from basivo_orch.auth.settings import Settings, get_settings

pytestmark = pytest.mark.security

SECRET = "webhook-signing-secret-" + "s" * 40


@pytest.fixture
def webhook_env(monkeypatch):
    """Configure the provider without touching the HTTP client."""
    monkeypatch.setenv("EMAIL_WEBHOOK_URL", "https://n8n.example.com/webhook/mail")
    monkeypatch.setenv("EMAIL_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def webhook(monkeypatch, webhook_env):
    """Point the sender at a fake endpoint and capture what it sends."""
    captured: dict[str, httpx.Request] = {}
    responses = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(responses["status"])

    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    # `_send_webhook` imports httpx inside the function, so patching the module
    # attribute reaches it at call time.
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield captured, responses


def an_email() -> sender.Email:
    return sender.Email(
        to="person@example.com",
        subject="Reset your password",
        html='<a href="https://app.example.com/auth/reset-password?token=SECRET-TOKEN">Reset</a>',
        text="https://app.example.com/auth/reset-password?token=SECRET-TOKEN",
    )


async def test_the_email_is_posted_to_the_configured_url(webhook) -> None:
    captured, _ = webhook
    assert await sender.send(an_email()) is True

    request = captured["request"]
    assert str(request.url) == "https://n8n.example.com/webhook/mail"
    assert request.method == "POST"

    body = json.loads(request.content)
    assert body["to"] == "person@example.com"
    assert body["subject"] == "Reset your password"
    assert "SECRET-TOKEN" in body["text"]


async def test_every_request_is_signed(webhook) -> None:
    """Without this, anyone who learns the URL can send mail as your domain.

    An automation webhook leaks the way URLs leak — browser history, a
    screenshot, an exported workflow. The signature is what lets the receiver
    tell your service apart from whoever found the link.
    """
    captured, _ = webhook
    await sender.send(an_email())
    request = captured["request"]

    signature = request.headers["X-Basivo-Signature"]
    timestamp = request.headers["X-Basivo-Timestamp"]
    assert signature.startswith("sha256=")

    expected = hmac.new(
        SECRET.encode(),
        timestamp.encode() + b"." + request.content,
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(signature.removeprefix("sha256="), expected)


async def test_the_signature_covers_the_exact_bytes_sent(webhook) -> None:
    """Re-serialising before sending would break verification intermittently.

    If the body were rebuilt after signing, a difference in key order or
    spacing would produce a valid-looking request that fails the receiver's
    check — and only sometimes, which is the worst way to find out.
    """
    captured, _ = webhook
    await sender.send(an_email())
    request = captured["request"]

    tampered = request.content.replace(b"person@example.com", b"attacker@evil.example")
    expected = hmac.new(
        SECRET.encode(),
        request.headers["X-Basivo-Timestamp"].encode() + b"." + tampered,
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-Basivo-Signature"].removeprefix("sha256=") != expected


async def test_the_body_is_canonical_so_another_language_can_rebuild_it(webhook) -> None:
    """Sorted keys, no spaces, real UTF-8.

    Verifying against the raw body is the correct approach, but not every
    receiver can reach it — n8n only exposes it behind an option. A canonical
    encoding means such a receiver can reproduce these exact bytes from the
    parsed JSON instead.

    `ensure_ascii=False` is the part that matters. With Python's default, an
    em-dash in an email template is written as \\u2014 while JavaScript emits
    the character literally, so the two sides disagree and every signature
    fails — but only on the messages that happen to contain one.
    """
    captured, _ = webhook
    email = sender.Email(
        to="person@example.com",
        subject="Reset your password — action needed",
        html="<p>Expires in 1 hour — act now</p>",
        text="Expires in 1 hour — act now",
    )
    await sender.send(email)

    raw = captured["request"].content
    assert b"\\u2014" not in raw, "non-ASCII was escaped; JavaScript will not match"
    assert "—".encode() in raw

    text = raw.decode("utf-8")
    assert ", " not in text.split('"html"')[0], "separators are not compact"

    keys = list(json.loads(text))
    assert keys == sorted(keys), "keys are not sorted"


async def test_a_timestamp_is_sent_so_replay_can_be_bounded(webhook) -> None:
    captured, _ = webhook
    await sender.send(an_email())
    assert captured["request"].headers["X-Basivo-Timestamp"].isdigit()


async def test_an_auth_header_is_sent_when_configured(webhook, monkeypatch) -> None:
    """n8n's Header Auth credential is the quickest way to lock a webhook down."""
    monkeypatch.setenv("EMAIL_WEBHOOK_AUTH_HEADER", "Bearer n8n-token")
    get_settings.cache_clear()

    captured, _ = webhook
    await sender.send(an_email())
    assert captured["request"].headers["Authorization"] == "Bearer n8n-token"


async def test_redirects_are_not_followed(webhook_env, monkeypatch) -> None:
    """A redirect would forward the body — containing a live reset link — to a
    host that was never configured here."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(307, headers={"location": "https://evil.example/collect"})
        return httpx.Response(200)

    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = await sender.send(an_email())

    assert len(seen) == 1, "the redirect was followed"
    assert seen[0].url.host == "n8n.example.com"
    # A 307 is not a success, so delivery is reported as failed rather than
    # silently counted as sent.
    assert result is False


async def test_a_failure_is_reported_not_raised(webhook) -> None:
    """A bounced email must not take down registration."""
    _, responses = webhook
    responses["status"] = 500
    assert await sender.send(an_email()) is False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = {
    "secret_key": "s" * 64,
    "environment": "production",
    "debug": False,
    "public_base_url": "https://api.example.com",
    "frontend_base_url": "https://app.example.com",
    "cors_origins": ["https://app.example.com"],
    "email_webhook_secret": SECRET,
}


def test_a_plaintext_webhook_url_is_rejected_in_production() -> None:
    """The body carries reset links. Over HTTP they are readable in transit,
    which is account takeover for every message sent."""
    with pytest.raises(ValidationError, match="https"):
        Settings(**{**BASE, "email_webhook_url": "http://n8n.example.com/webhook/mail"})


def test_a_missing_webhook_url_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="EMAIL_WEBHOOK_URL"):
        Settings(**{**BASE, "email_webhook_url": ""})


def test_an_unauthenticated_webhook_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="EMAIL_WEBHOOK_SECRET"):
        Settings(
            **{
                **BASE,
                "email_webhook_url": "https://n8n.example.com/webhook/mail",
                "email_webhook_secret": "",
                "email_webhook_auth_header": "",
            }
        )


def test_an_auth_header_alone_satisfies_the_requirement() -> None:
    settings = Settings(
        **{
            **BASE,
            "email_webhook_url": "https://n8n.example.com/webhook/mail",
            "email_webhook_secret": "",
            "email_webhook_auth_header": "Bearer token",
        }
    )
    assert settings.email_webhook_url.startswith("https://")
