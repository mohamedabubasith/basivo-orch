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
from basivo_orch.flows.nodes.social import (
    MEDIA_LIMITS,
    SocialPostConfig,
    SocialPostNode,
    sniff_media,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"poster-bytes"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"
GIF = b"GIF89a" + b"gif-bytes"
#: A real MP4 begins with a box length, then "ftyp", then the major brand.
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-bytes"
MOV = b"\x00\x00\x00\x14ftypqt  " + b"video-bytes"
WEBM = b"\x1a\x45\xdf\xa3" + b"matroska-bytes"


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
    with pytest.raises(ValueError, match="text, a file, or both"):
        SocialPostConfig(platform="telegram", credential_id="c", target="@c")


def test_posting_is_never_replayed():
    """A retry after a timeout publishes a second time. Recovery must not."""
    assert SocialPostNode.replay_safe is False
    assert SocialPostNode.max_attempts == 1


# ---------------------------------------------------------------------------
# What the file actually is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, ("image", "image/png")),
        (JPEG, ("image", "image/jpeg")),
        (GIF, ("gif", "image/gif")),
        (MP4, ("video", "video/mp4")),
        (MOV, ("video", "video/quicktime")),
        (WEBM, ("video", "video/webm")),
    ],
)
def test_the_sniffer_names_the_container_from_the_bytes(data: bytes, expected: tuple[str, str]):
    """The file name is the part most likely to be wrong, so it is not read."""
    assert sniff_media(data) == expected


def test_a_file_too_short_to_identify_is_refused_rather_than_guessed():
    with pytest.raises(NodeError, match="not an image or a video"):
        sniff_media(b"ftyp")


async def test_an_unidentifiable_file_is_refused_before_anything_is_sent():
    async with httpx.AsyncClient() as http:
        ctx = make_context(_Recorder(), http, provider="telegram", artifact=b"not media at all")
        with pytest.raises(NodeError, match="PNG, JPEG, GIF, MP4, MOV or WebM"):
            await SocialPostNode().run(
                SocialPostConfig(
                    platform="telegram",
                    credential_id="cred",
                    text="hi",
                    target="@c",
                    artifact_id="art-1",
                ),
                ctx,
            )


async def test_a_file_over_the_platform_limit_names_the_limit_and_the_size():
    """Discord's webhook takes 8 MB. A 9 MB video should not be uploaded to
    find that out."""

    oversized = MP4 + b"x" * (MEDIA_LIMITS["discord"] + 1 - len(MP4))
    async with httpx.AsyncClient() as http:
        ctx = make_context(
            _Recorder(),
            http,
            provider="discord",
            api_key="https://discord.com/api/webhooks/1/abc",
            artifact=oversized,
        )
        with pytest.raises(NodeError, match=r"up to 8\.0 MB and this file is 8\.0 MB"):
            await SocialPostNode().run(
                SocialPostConfig(
                    platform="discord", credential_id="cred", text="hi", artifact_id="art-1"
                ),
                ctx,
            )


# ---------------------------------------------------------------------------
# Video, per platform
# ---------------------------------------------------------------------------


async def test_telegram_sends_a_video_to_sendvideo_and_asks_for_streaming():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendVideo"), "a video must not go to sendPhoto"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5, "chat": {}}})

    result, requests, recorder = await run_post(
        SocialPostConfig(
            platform="telegram",
            credential_id="cred",
            text="the render is up",
            artifact_id="art-1",
            target="@basivo",
        ),
        handler,
        provider="telegram",
        artifact=MP4,
    )

    body = requests[0].content
    assert b'name="video"' in body, "the file field must match the endpoint"
    assert b'filename="post.mp4"' in body
    assert b"video/mp4" in body
    assert b"supports_streaming" in body, "without it the channel offers a download"
    assert MP4 in body
    assert result.output["id"] == "5"
    assert recorder.data_for("post.started")[0]["media"] == "video"


async def test_telegram_sends_a_gif_to_sendanimation():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendAnimation")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 6, "chat": {}}})

    _, requests, _ = await run_post(
        SocialPostConfig(
            platform="telegram",
            credential_id="cred",
            text="loop",
            artifact_id="art-1",
            target="@basivo",
        ),
        handler,
        provider="telegram",
        artifact=GIF,
    )
    assert b'name="animation"' in requests[0].content
    assert b'filename="post.gif"' in requests[0].content


async def test_discord_sends_the_video_under_its_real_name_and_type():
    """Discord embeds a player from the extension and content type; poster.png
    on an MP4 is an attachment nobody can play."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "42"})

    _, requests, _ = await run_post(
        SocialPostConfig(
            platform="discord", credential_id="cred", text="new clip", artifact_id="art-1"
        ),
        handler,
        provider="discord",
        api_key="https://discord.com/api/webhooks/1/abc",
        artifact=WEBM,
    )
    body = requests[0].content
    assert b'filename="post.webm"' in body
    assert b"video/webm" in body
    assert WEBM in body


async def test_slack_refuses_a_video_the_same_way_it_refuses_an_image():
    """An incoming webhook carries no upload of any kind. Better a clear
    refusal than a post that silently loses the video."""

    async with httpx.AsyncClient() as http:
        ctx = make_context(
            _Recorder(),
            http,
            provider="slack",
            api_key="https://hooks.slack.com/services/x",
            artifact=MP4,
        )
        with pytest.raises(NodeError, match="cannot attach files"):
            await SocialPostNode().run(
                SocialPostConfig(
                    platform="slack", credential_id="cred", text="hi", artifact_id="art-1"
                ),
                ctx,
            )


async def test_mastodon_waits_for_a_video_that_is_still_processing():
    """202 means the id exists but attaching it now is rejected."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/v2/media":
            assert b'filename="post.mp4"' in request.content
            assert b"video/mp4" in request.content
            return httpx.Response(202, json={"id": "media-9"})
        if request.url.path == "/api/v1/media/media-9":
            return httpx.Response(200, json={"id": "media-9", "url": "https://m.social/v.mp4"})
        assert json.loads(request.content)["media_ids"] == ["media-9"]
        return httpx.Response(200, json={"id": "s2", "url": "https://m.social/@me/s2"})

    result, _, _ = await run_post(
        SocialPostConfig(
            platform="mastodon", credential_id="cred", text="rendered", artifact_id="art-1"
        ),
        handler,
        provider="mastodon",
        base_url="https://m.social",
        artifact=MP4,
    )

    assert calls == [
        "POST /api/v2/media",
        "GET /api/v1/media/media-9",
        "POST /api/v1/statuses",
    ]
    assert result.output["url"] == "https://m.social/@me/s2"


async def test_bluesky_refuses_a_video_before_it_uploads_anything():
    """The blob upload would accept an MP4 and post an image embed that will
    not play, so the refusal comes before the session is created."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"Bluesky was called: {request.url}")

    with pytest.raises(NodeError, match="images only, not video"):
        await run_post(
            SocialPostConfig(
                platform="bluesky", credential_id="cred", text="clip", artifact_id="art-1"
            ),
            handler,
            provider="bluesky",
            options={"identifier": "me.bsky.social"},
            artifact=MP4,
        )
