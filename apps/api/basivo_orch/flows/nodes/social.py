"""Posting to the platforms that let you post for free.

Every platform here is reachable with a credential the user creates in a
couple of minutes, costs nothing per post, and needs no app review:

| Platform | Credential | Where to get it |
|---|---|---|
| Telegram | bot token | @BotFather, then add the bot to the channel |
| Discord | webhook URL | channel settings → Integrations → Webhooks |
| Slack | webhook URL | Slack app → Incoming Webhooks |
| Mastodon | access token | Preferences → Development → New application |
| Bluesky | app password | Settings → App Passwords |

Deliberately not here: **X**, which since February 2026 is pay-per-use at
about $0.20 a post, and **Instagram, Facebook, LinkedIn and TikTok**, whose
APIs are free but require the user to register their own app and pass a
review. Those belong behind a different door — a node that pretends a review
queue does not exist would fail for every new user on their first run.

Uploads are direct rather than by URL. Every platform here accepts the bytes,
which means a poster can be posted from a machine with no public address —
no bucket, no signed link, nothing to expire.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

Platform = Literal["telegram", "discord", "slack", "mastodon", "bluesky"]

MediaKind = Literal["image", "video", "gif"]

#: Which platforms need somewhere to post *to* beyond the credential itself.
NEEDS_TARGET: dict[Platform, str] = {
    "telegram": "The channel or chat id, e.g. @mychannel or -1001234567890.",
}

#: Caption ceilings, enforced before sending so the failure names the limit
#: rather than arriving as a provider error nobody can act on.
TEXT_LIMITS: dict[Platform, int] = {
    "telegram": 1024,  # caption limit, the same for a photo, a video or a GIF
    "discord": 2000,
    "slack": 3000,
    "mastodon": 500,
    "bluesky": 300,
}

#: Upload ceilings in bytes, from each platform's own documentation: Telegram
#: Bot API "sending files" (50 MB per upload; photos are capped lower at 10 MB,
#: which we leave to Telegram to reject), Discord's 8 MB attachment limit on an
#: unboosted server, Slack's 1 GB per file, and Mastodon's default 40 MB video
#: (flagship configuration). A server may be stricter — a self-hosted Mastodon
#: with a smaller `MAX_VIDEO_SIZE`, a Discord webhook in a boosted guild is
#: looser — so this catches the common case early and a provider that refuses
#: anyway still comes back through `_ok` in its own words.
MEDIA_LIMITS: dict[Platform, int] = {
    "telegram": 50 * 1024 * 1024,
    "discord": 8 * 1024 * 1024,
    "slack": 1024 * 1024 * 1024,
    "mastodon": 40 * 1024 * 1024,
    "bluesky": 1024 * 1024,  # blob limit for an image; video is refused outright
}

#: What the upload is called. Providers key their preview and thumbnail
#: handling off the extension, so an MP4 sent as "poster.png" arrives as an
#: attachment nobody can play.
_FILENAMES: dict[str, str] = {
    "video/mp4": "post.mp4",
    "video/quicktime": "post.mov",
    "video/webm": "post.webm",
    "image/gif": "post.gif",
    "image/png": "post.png",
    "image/jpeg": "post.jpg",
}


@dataclass(frozen=True, slots=True)
class Attachment:
    """The bytes plus what they actually are, decided once for every platform."""

    data: bytes
    kind: MediaKind
    content_type: str

    @property
    def filename(self) -> str:
        return _FILENAMES[self.content_type]


def sniff_media(data: bytes) -> tuple[MediaKind, str]:
    """Identify bytes by their magic number rather than by a file name.

    Artifacts arrive as bytes with a name that a render node chose, and the
    name is the thing most likely to be wrong: a template that produced WebM
    while the config said mp4, an agent that copied a filename. Telegram
    rejects a video sent to sendPhoto and Discord happily posts a file nobody
    can open, so the container decides which endpoint is used.
    """
    if len(data) >= 12 and data[4:8] == b"ftyp":
        # MP4 and MOV are the same ISO base media container; the major brand
        # is what separates them, and QuickTime needs its own content type or
        # Telegram treats the upload as a plain document.
        return "video", "video/quicktime" if data[8:10] == b"qt" else "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"  # EBML: WebM and Matroska both
    if data.startswith(b"GIF8"):
        return "gif", "image/gif"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    raise NodeError(
        "That file is not an image or a video this node can post. Attach a PNG, JPEG, GIF, "
        "MP4, MOV or WebM."
    )


class SocialPostConfig(BaseModel):
    model_config = {"extra": "forbid"}

    platform: Platform = "telegram"
    credential_id: str = Field(
        default="", title="Credential", description="A saved credential for this platform."
    )
    text: str = Field(
        default="",
        max_length=20_000,
        description="The post. Supports {{ references }}.",
    )
    #: Usually `{{ nodes.poster.output.artifact_id }}` — what the render node
    #: produced. Empty posts text only.
    artifact_id: str = Field(
        default="",
        max_length=200,
        description="An image or video to attach, by artifact id.",
    )
    target: str = Field(
        default="", max_length=200, description="Channel or chat id, where the platform needs one."
    )
    alt_text: str = Field(
        default="",
        max_length=1000,
        description=(
            "Describes the image or video for screen readers. Bluesky and Mastodon show it."
        ),
    )

    @model_validator(mode="after")
    def _needs_something_to_post(self) -> SocialPostConfig:
        if not self.text.strip() and not self.artifact_id.strip():
            raise ValueError("A post needs text, a file, or both.")
        return self


class SocialPostNode(Node):
    """One node, several platforms, one credential each."""

    type = "social.post"
    label = "Post to Social"
    description = (
        "Post text with an image or a video to Telegram, Discord, Slack, Mastodon or Bluesky."
    )
    when = (
        "The result of a flow should be published somewhere people read: a poster, a rendered "
        "video, or text on its own. Not for replying to the person who messaged your bot; use "
        "Telegram Reply for that."
    )
    needs = (
        "A credential for the target network saved under Credentials.",
        "A trigger before it, or any node whose output it should work on",
    )
    example = "Schedule -> Write with AI -> AI Video -> Post to Social"
    tier = 2
    category = "social"
    config_model = SocialPostConfig
    output_paths = ("url", "id", "platform")
    #: Posting twice is worse than not posting: a retry that succeeds after a
    #: timeout has already published once.
    max_attempts = 1
    replay_safe = False
    timeout_seconds = 120.0

    async def run(self, config: SocialPostConfig, ctx: NodeContext) -> NodeResult:
        template = ctx.template_context()
        text = str(render_value(config.text, template)) if config.text else ""
        target = str(render_value(config.target, template)) if config.target else ""
        artifact_id = str(render_value(config.artifact_id, template)) if config.artifact_id else ""

        limit = TEXT_LIMITS[config.platform]
        if len(text) > limit:
            raise NodeError(
                f"{config.platform} allows {limit} characters and this post is {len(text)}. "
                "Shorten it, or ask the agent that wrote it for a shorter version."
            )

        if config.platform in NEEDS_TARGET and not target:
            raise NodeError(f"{config.platform} needs a target. {NEEDS_TARGET[config.platform]}")

        credential = None
        if config.credential_id:
            credential = await ctx.resolve_credential(config.credential_id)
        if credential is None:
            raise NodeError(
                f"Pick a saved {config.platform} credential on this node. Posting needs one."
            )
        if credential.provider != config.platform:
            raise NodeError(
                f"That credential is for {credential.provider!r}, not {config.platform!r}."
            )

        media: Attachment | None = None
        if artifact_id:
            if ctx.load_artifact is None:  # pragma: no cover - engine always provides it
                raise NodeError("This run cannot read files.")
            data = await ctx.load_artifact(artifact_id)
            if data is None:
                raise NodeError(
                    f"No file with id {artifact_id!r} in this workspace. Check the reference: "
                    "it usually comes from a render node's artifact_id."
                )
            kind, content_type = sniff_media(data)
            media = Attachment(data=data, kind=kind, content_type=content_type)

            size_limit = MEDIA_LIMITS[config.platform]
            if len(data) > size_limit:
                raise NodeError(
                    f"{config.platform} accepts uploads up to {_mb(size_limit)} and this file "
                    f"is {_mb(len(data))}. Render it shorter or smaller, or post a link to it."
                )
            if kind == "video" and config.platform == "bluesky":
                # Refused here, before createSession and before uploadBlob:
                # com.atproto.repo.uploadBlob would take the MP4 and the post
                # would appear with an image embed pointing at an unplayable
                # blob. Bluesky video needs a whole other upload service.
                raise NodeError(
                    "Bluesky posts here support images only, not video. Post the video to "
                    "Telegram, Discord or Mastodon instead."
                )

        await ctx.step(
            "post.started",
            {
                "platform": config.platform,
                "media": media.kind if media else "none",
                "characters": len(text),
                "target": target[:80],
            },
        )
        await ctx.progress(f"Posting to {config.platform}")

        poster = {
            "telegram": _post_telegram,
            "discord": _post_discord,
            "slack": _post_slack,
            "mastodon": _post_mastodon,
            "bluesky": _post_bluesky,
        }[config.platform]

        result = await poster(
            ctx.http,
            credential=credential,
            text=text,
            media=media,
            target=target,
            alt_text=config.alt_text,
        )
        result["platform"] = config.platform
        await ctx.step("post.published", result)
        return NodeResult(output=result)


def _mb(size: int) -> str:
    """Sizes a person can compare. Bytes in an error message are arithmetic."""
    return f"{size / (1024 * 1024):.1f} MB"


async def _ok(response: httpx.Response, platform: str) -> dict[str, Any]:
    if response.status_code >= 400:
        # The platform's own words: "chat not found" is actionable, "400" is not.
        raise NodeError(
            f"{platform} refused the post ({response.status_code}): {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError:
        return {}


async def _post_telegram(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    media: Attachment | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    base = (credential.base_url or "https://api.telegram.org").rstrip("/")
    api = f"{base}/bot{credential.api_key}"
    if media:
        # One endpoint per kind, and the field name has to match it: an MP4
        # sent to sendPhoto comes back "IMAGE_PROCESS_FAILED", and a GIF sent
        # to sendVideo loses the loop and arrives as a one second clip.
        method, field = {
            "video": ("sendVideo", "video"),
            "gif": ("sendAnimation", "animation"),
            "image": ("sendPhoto", "photo"),
        }[media.kind]
        form = {"chat_id": target, "caption": text, "parse_mode": "HTML"}
        if media.kind == "video":
            # Without this the channel offers the file for download instead of
            # playing it where it was posted.
            form["supports_streaming"] = "true"
        response = await http.post(
            f"{api}/{method}",
            data=form,
            files={field: (media.filename, media.data, media.content_type)},
        )
    else:
        response = await http.post(
            f"{api}/sendMessage",
            data={"chat_id": target, "text": text, "parse_mode": "HTML"},
        )
    payload = await _ok(response, "Telegram")
    message = payload.get("result", {})
    chat = message.get("chat", {})
    username = chat.get("username")
    message_id = message.get("message_id")
    return {
        "id": str(message_id or ""),
        "url": f"https://t.me/{username}/{message_id}" if username and message_id else "",
    }


async def _post_discord(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    media: Attachment | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    # The credential *is* the webhook URL: Discord webhooks carry their own
    # secret in the path and need no other auth.
    webhook = (credential.base_url or credential.api_key).strip()
    if not webhook.startswith("https://"):
        raise NodeError("A Discord credential must be the full webhook URL.")
    if media:
        # Discord takes any attachment; it embeds a player from the extension
        # and content type, so those are the whole difference between a video
        # that plays in the channel and one that has to be downloaded first.
        response = await http.post(
            f"{webhook}?wait=true",
            data={"payload_json": json.dumps({"content": text})},
            files={"files[0]": (media.filename, media.data, media.content_type)},
        )
    else:
        response = await http.post(f"{webhook}?wait=true", json={"content": text})
    payload = await _ok(response, "Discord")
    return {"id": str(payload.get("id", "")), "url": ""}


async def _post_slack(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    media: Attachment | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    webhook = (credential.base_url or credential.api_key).strip()
    if not webhook.startswith("https://"):
        raise NodeError("A Slack credential must be the full incoming-webhook URL.")
    if media:
        # Slack's incoming webhooks carry no file upload of any kind, image or
        # video; that needs a bot token and a three-call dance. Said plainly
        # rather than silently dropping the file.
        raise NodeError(
            "Slack incoming webhooks cannot attach files. Post the text here, or use "
            "Telegram, Discord or Mastodon for the image or video."
        )
    response = await http.post(webhook, json={"text": text})
    if response.status_code >= 400:
        raise NodeError(f"Slack refused the post ({response.status_code}): {response.text[:200]}")
    return {"id": "", "url": ""}


async def _post_mastodon(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    media: Attachment | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    base = (credential.base_url or "https://mastodon.social").rstrip("/")
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    media_ids: list[str] = []
    if media:
        upload = await http.post(
            f"{base}/api/v2/media",
            headers=headers,
            files={"file": (media.filename, media.data, media.content_type)},
            data={"description": alt_text} if alt_text else None,
        )
        uploaded = await _ok(upload, "Mastodon")
        media_id = str(uploaded.get("id") or "")
        if media_id:
            media_ids.append(media_id)
        if upload.status_code == 202 and media_id:
            await _await_mastodon_processing(http, base, headers, media_id)

    response = await http.post(
        f"{base}/api/v1/statuses",
        headers=headers,
        json={"status": text, "media_ids": media_ids} if media_ids else {"status": text},
    )
    payload = await _ok(response, "Mastodon")
    return {"id": str(payload.get("id", "")), "url": payload.get("url", "")}


#: How long to wait for Mastodon to finish transcoding, and how often to ask.
#: Twenty seconds covers the short clips this node posts while leaving room
#: under the node's own 120 second timeout for the status call itself.
MASTODON_PROCESSING_TRIES = 10
MASTODON_PROCESSING_INTERVAL = 2.0


async def _await_mastodon_processing(
    http: httpx.AsyncClient, base: str, headers: dict[str, str], media_id: str
) -> None:
    """Wait for an asynchronously processed upload to become attachable.

    Mastodon transcodes video in the background and answers the upload with
    202 and an id that is not ready yet. Posting a status with that id is
    rejected ("Cannot attach files that have not finished processing"), so the
    two-step upload becomes a three-step one for video: poll until the media
    endpoint answers 200, which is Mastodon's own signal that it is done.
    """
    for attempt in range(MASTODON_PROCESSING_TRIES):
        if attempt:
            await asyncio.sleep(MASTODON_PROCESSING_INTERVAL)
        check = await http.get(f"{base}/api/v1/media/{media_id}", headers=headers)
        if check.status_code == 200:
            return
        if check.status_code >= 400:
            raise NodeError(
                f"Mastodon refused the upload ({check.status_code}): {check.text[:300]}"
            )
    waited = int(MASTODON_PROCESSING_TRIES * MASTODON_PROCESSING_INTERVAL)
    raise NodeError(
        f"Mastodon was still processing the video after {waited} seconds, so the post was not "
        "sent. Try again, or post a shorter video."
    )


async def _post_bluesky(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    media: Attachment | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    base = (credential.base_url or "https://bsky.social").rstrip("/")
    handle = str((credential.options or {}).get("identifier") or "").strip()
    if not handle:
        raise NodeError(
            "A Bluesky credential needs the account handle as well as the app password. "
            "Set 'identifier' in the credential's options, e.g. yourname.bsky.social."
        )

    session = await _ok(
        await http.post(
            f"{base}/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": credential.api_key},
        ),
        "Bluesky",
    )
    jwt = session.get("accessJwt")
    did = session.get("did")
    if not jwt or not did:
        raise NodeError("Bluesky did not return a session. Check the handle and app password.")
    headers = {"Authorization": f"Bearer {jwt}"}

    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        # Bluesky requires an ISO timestamp it can sort on; the server's clock
        # is the honest one here.
        "createdAt": _now_iso(),
    }
    if media:
        # Video never reaches here: `run` refuses it before the session is
        # even created, because this blob upload would accept the bytes and
        # produce a post with an image embed that will not play.
        blob = await _ok(
            await http.post(
                f"{base}/xrpc/com.atproto.repo.uploadBlob",
                headers={**headers, "Content-Type": media.content_type},
                content=media.data,
            ),
            "Bluesky",
        )
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": [{"alt": alt_text or "", "image": blob.get("blob")}],
        }

    payload = await _ok(
        await http.post(
            f"{base}/xrpc/com.atproto.repo.createRecord",
            headers=headers,
            json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
        ),
        "Bluesky",
    )
    uri = str(payload.get("uri", ""))
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    return {
        "id": uri,
        "url": f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else "",
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
