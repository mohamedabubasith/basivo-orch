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

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

Platform = Literal["telegram", "discord", "slack", "mastodon", "bluesky"]

#: Which platforms need somewhere to post *to* beyond the credential itself.
NEEDS_TARGET: dict[Platform, str] = {
    "telegram": "The channel or chat id, e.g. @mychannel or -1001234567890.",
}

#: Caption ceilings, enforced before sending so the failure names the limit
#: rather than arriving as a provider error nobody can act on.
TEXT_LIMITS: dict[Platform, int] = {
    "telegram": 1024,  # caption limit when a photo is attached
    "discord": 2000,
    "slack": 3000,
    "mastodon": 500,
    "bluesky": 300,
}


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
        default="", max_length=200, description="An image to attach, by artifact id."
    )
    target: str = Field(
        default="", max_length=200, description="Channel or chat id, where the platform needs one."
    )
    alt_text: str = Field(
        default="",
        max_length=1000,
        description="Describes the image for screen readers. Bluesky and Mastodon show it.",
    )

    @model_validator(mode="after")
    def _needs_something_to_post(self) -> SocialPostConfig:
        if not self.text.strip() and not self.artifact_id.strip():
            raise ValueError("A post needs text, an image, or both.")
        return self


class SocialPostNode(Node):
    """One node, several platforms, one credential each."""

    type = "social.post"
    label = "Post to Social"
    description = "Post text and an image to Telegram, Discord, Slack, Mastodon or Bluesky."
    when = (
        "The result of a flow should be published somewhere people read. Not for replying to "
        "the person who messaged your bot; use Telegram Reply for that."
    )
    needs = (
        "A credential for the target network saved under Credentials.",
        "A trigger before it, or any node whose output it should work on",
    )
    example = "Schedule -> AI Agent -> HTML to Image -> Post to Social"
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

        image: bytes | None = None
        if artifact_id:
            if ctx.load_artifact is None:  # pragma: no cover - engine always provides it
                raise NodeError("This run cannot read files.")
            image = await ctx.load_artifact(artifact_id)
            if image is None:
                raise NodeError(
                    f"No file with id {artifact_id!r} in this workspace. Check the reference: "
                    "it usually comes from a render node's artifact_id."
                )

        await ctx.step(
            "post.started",
            {
                "platform": config.platform,
                "has_image": image is not None,
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
            image=image,
            target=target,
            alt_text=config.alt_text,
        )
        result["platform"] = config.platform
        await ctx.step("post.published", result)
        return NodeResult(output=result)


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
    image: bytes | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    base = (credential.base_url or "https://api.telegram.org").rstrip("/")
    api = f"{base}/bot{credential.api_key}"
    if image:
        response = await http.post(
            f"{api}/sendPhoto",
            data={"chat_id": target, "caption": text, "parse_mode": "HTML"},
            files={"photo": ("poster.png", image, "image/png")},
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
    image: bytes | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    # The credential *is* the webhook URL: Discord webhooks carry their own
    # secret in the path and need no other auth.
    webhook = (credential.base_url or credential.api_key).strip()
    if not webhook.startswith("https://"):
        raise NodeError("A Discord credential must be the full webhook URL.")
    if image:
        response = await http.post(
            f"{webhook}?wait=true",
            data={"payload_json": json.dumps({"content": text})},
            files={"files[0]": ("poster.png", image, "image/png")},
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
    image: bytes | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    webhook = (credential.base_url or credential.api_key).strip()
    if not webhook.startswith("https://"):
        raise NodeError("A Slack credential must be the full incoming-webhook URL.")
    if image:
        # Slack's incoming webhooks carry no file upload; that needs a bot
        # token and a three-call dance. Said plainly rather than silently
        # dropping the poster.
        raise NodeError(
            "Slack incoming webhooks cannot attach files. Post the text here, or use "
            "Telegram/Discord/Mastodon/Bluesky for the image."
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
    image: bytes | None,
    target: str,
    alt_text: str,
) -> dict[str, Any]:
    base = (credential.base_url or "https://mastodon.social").rstrip("/")
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    media_ids: list[str] = []
    if image:
        upload = await http.post(
            f"{base}/api/v2/media",
            headers=headers,
            files={"file": ("poster.png", image, "image/png")},
            data={"description": alt_text} if alt_text else None,
        )
        media = await _ok(upload, "Mastodon")
        if media.get("id"):
            media_ids.append(str(media["id"]))

    response = await http.post(
        f"{base}/api/v1/statuses",
        headers=headers,
        json={"status": text, "media_ids": media_ids} if media_ids else {"status": text},
    )
    payload = await _ok(response, "Mastodon")
    return {"id": str(payload.get("id", "")), "url": payload.get("url", "")}


async def _post_bluesky(
    http: httpx.AsyncClient,
    *,
    credential: Any,
    text: str,
    image: bytes | None,
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
    if image:
        blob = await _ok(
            await http.post(
                f"{base}/xrpc/com.atproto.repo.uploadBlob",
                headers={**headers, "Content-Type": "image/png"},
                content=image,
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
