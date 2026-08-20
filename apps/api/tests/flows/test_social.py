"""Posting, asserted on the wire.

Each platform is served by a MockTransport that records what was actually
sent, because that is where posting bugs live: the right bytes to the wrong
field, a caption where a body belongs, an image silently dropped. Nothing here
talks to a real network, and the live path is a token the user supplies.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError, ResolvedCredential
from basivo_orch.flows.nodes.social import SocialPostConfig, SocialPostNode

PNG = b"\x89PNG\r\n\x1a\n" + b"poster-bytes"


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(
    recorder: _Recorder,
    http: httpx.AsyncClient,
    *,
    provider: str,
    api_key: str = "secret-token",
    base_url: str | None = None,
    options: dict | None = None,
    artifact: bytes | None = PNG,
) -> NodeContext:
    async def resolve_credential(credential_id: str):
        if credential_id == "cred":
            return ResolvedCredential(
                provider=provider, api_key=api_key, base_url=base_url, options=options or {}
            )
        return None

    async def load_artifact(artifact_id: str):
        return artifact if artifact_id == "art-1" else None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="post",
        node_name="Post",
        attempt=1,
        input={"headline": "Ship it"},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
        load_artifact=load_artifact,
    )


async def run_post(config: SocialPostConfig, handler, **context_kwargs):
    recorder = _Recorder()
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as http:
        ctx = make_context(recorder, http, **context_kwargs)
        result = await SocialPostNode().run(config, ctx)
    return result, requests, recorder


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


async def test_telegram_sends_the_poster_as_a_photo_with_a_caption():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendPhoto")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 12, "chat": {"username": "basivo"}},
            },
        )

    result, requests, recorder = await run_post(
        SocialPostConfig(
            platform="telegram",
            credential_id="cred",
            text="Today: {{ input.headline }}",
            artifact_id="art-1",
            target="@basivo",
        ),
        handler,
        provider="telegram",
    )

    body = requests[0].content
    assert b"Today: Ship it" in body, "the caption was not templated or not sent"
    assert PNG in body, "the poster bytes never reached Telegram"
    assert result.output["url"] == "https://t.me/basivo/12"
    assert recorder.data_for("post.published")[0]["platform"] == "telegram"


async def test_telegram_without_an_image_sends_a_message():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3, "chat": {}}})

    result, _, _ = await run_post(
        SocialPostConfig(platform="telegram", credential_id="cred", text="text only", target="@c"),
        handler,
        provider="telegram",
    )
    assert result.output["id"] == "3"


async def test_telegram_without_a_target_says_which_one_it_needs():
    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="telegram")
        with pytest.raises(NodeError, match="needs a target"):
            await SocialPostNode().run(
                SocialPostConfig(platform="telegram", credential_id="cred", text="hi"), ctx
            )


# ---------------------------------------------------------------------------
# Discord, Slack
# ---------------------------------------------------------------------------


async def test_discord_posts_the_file_through_the_webhook():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "discord.com"
        return httpx.Response(200, json={"id": "999"})

    result, requests, _ = await run_post(
        SocialPostConfig(
            platform="discord", credential_id="cred", text="new poster", artifact_id="art-1"
        ),
        handler,
        provider="discord",
        api_key="https://discord.com/api/webhooks/1/abc",
    )
    assert PNG in requests[0].content
    assert b"new poster" in requests[0].content
    assert result.output["id"] == "999"


async def test_slack_says_plainly_that_a_webhook_cannot_carry_a_file():
    """Better a clear refusal than a post that silently loses the poster."""

    async with httpx.AsyncClient() as http:
        ctx = make_context(
            _Recorder(), http, provider="slack", api_key="https://hooks.slack.com/services/x"
        )
        with pytest.raises(NodeError, match="cannot attach files"):
            await SocialPostNode().run(
                SocialPostConfig(
                    platform="slack", credential_id="cred", text="hi", artifact_id="art-1"
                ),
                ctx,
            )


# ---------------------------------------------------------------------------
# Mastodon, Bluesky
# ---------------------------------------------------------------------------


async def test_mastodon_uploads_the_media_then_attaches_it_to_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/media":
            return httpx.Response(200, json={"id": "media-7"})
        assert request.url.path == "/api/v1/statuses"
        assert json.loads(request.content)["media_ids"] == ["media-7"]
        return httpx.Response(200, json={"id": "s1", "url": "https://m.social/@me/s1"})

    result, requests, _ = await run_post(
        SocialPostConfig(
            platform="mastodon",
            credential_id="cred",
            text="hello fediverse",
            artifact_id="art-1",
            alt_text="A poster",
        ),
        handler,
        provider="mastodon",
        base_url="https://m.social",
    )

    assert len(requests) == 2, "media upload and status must be separate calls"
    assert result.output["url"] == "https://m.social/@me/s1"


async def test_bluesky_authenticates_uploads_the_blob_then_creates_the_record():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:me"})
        if request.url.path.endswith("uploadBlob"):
            return httpx.Response(200, json={"blob": {"$type": "blob", "ref": {"$link": "cid"}}})
        payload = json.loads(request.content)
        assert payload["record"]["embed"]["images"][0]["alt"] == "A poster"
        return httpx.Response(200, json={"uri": "at://did:plc:me/app.bsky.feed.post/abc123"})

    result, _, _ = await run_post(
        SocialPostConfig(
            platform="bluesky",
            credential_id="cred",
            text="hello sky",
            artifact_id="art-1",
            alt_text="A poster",
        ),
        handler,
        provider="bluesky",
        options={"identifier": "me.bsky.social"},
    )

    assert calls == [
        "/xrpc/com.atproto.server.createSession",
        "/xrpc/com.atproto.repo.uploadBlob",
        "/xrpc/com.atproto.repo.createRecord",
    ]
    assert result.output["url"] == "https://bsky.app/profile/me.bsky.social/post/abc123"


async def test_bluesky_without_a_handle_explains_what_is_missing():
    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="bluesky")
        with pytest.raises(NodeError, match="handle"):
            await SocialPostNode().run(
                SocialPostConfig(platform="bluesky", credential_id="cred", text="hi"), ctx
            )


# ---------------------------------------------------------------------------
# The checks that apply everywhere
# ---------------------------------------------------------------------------


async def test_a_credential_for_another_platform_is_refused():
    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="discord")
        with pytest.raises(NodeError, match="not 'telegram'"):
            await SocialPostNode().run(
                SocialPostConfig(platform="telegram", credential_id="cred", text="hi", target="@c"),
                ctx,
            )


async def test_a_post_too_long_for_the_platform_is_caught_before_sending():
    """Bluesky allows 300 characters. Learning that from the API's rejection
    after the image was already uploaded helps nobody."""

    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="bluesky")
        with pytest.raises(NodeError, match="300 characters"):
            await SocialPostNode().run(
                SocialPostConfig(platform="bluesky", credential_id="cred", text="x" * 301), ctx
            )


async def test_a_missing_image_names_the_reference_rather_than_failing_blankly():
    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="telegram")
        with pytest.raises(NodeError, match="No file with id"):
            await SocialPostNode().run(
                SocialPostConfig(
                    platform="telegram",
                    credential_id="cred",
                    text="hi",
                    target="@c",
                    artifact_id="does-not-exist",
                ),
                ctx,
            )


def test_a_post_must_contain_something():
    with pytest.raises(ValueError, match="text, an image, or both"):
        SocialPostConfig(platform="telegram", credential_id="c", target="@c")


def test_posting_is_never_replayed():
    """A retry after a timeout publishes a second time. Recovery must not."""
    assert SocialPostNode.replay_safe is False
    assert SocialPostNode.max_attempts == 1
