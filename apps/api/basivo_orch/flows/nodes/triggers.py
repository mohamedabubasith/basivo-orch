"""How a flow starts.

Triggers do no work. They exist so the graph has a single, explicit entry point
and so the run log records what set the flow off — which is the difference
between "this failed" and "this failed every time the 6am schedule fired".
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult

WebhookMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
TelegramUpdateKind = Literal["message", "photo", "document", "audio", "callback_query"]
DEFAULT_WEBHOOK_METHODS: list[WebhookMethod] = ["POST"]


class ManualTriggerConfig(BaseModel):
    """Nothing to configure. Declared so validation treats it like any other node."""

    model_config = {"extra": "forbid"}


class ManualTriggerNode(Node):
    type = "trigger.manual"
    label = "Manual Trigger"
    description = "Run on demand from the UI or the API."
    tier = 1
    category = "trigger"
    is_trigger = True
    config_model = ManualTriggerConfig

    async def run(self, config: ManualTriggerConfig, ctx: NodeContext) -> NodeResult:
        # The trigger's output *is* the run input, so the first real node can
        # read `{{ input.whatever }}` without knowing how the run started.
        return NodeResult(output=ctx.trigger.get("payload", {}))


class WebhookTriggerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    methods: list[WebhookMethod] = Field(default_factory=lambda: list(DEFAULT_WEBHOOK_METHODS))
    #: Demand `X-Webhook-Secret` on every inbound call, checked at the edge —
    #: before a run is created — so a caller who has the URL but not the secret
    #: cannot fill the run table by spraying it. Enforced in the external
    #: router; see `verify_webhook_secret`.
    require_signature: bool = False
    secret: str = Field(
        default="",
        max_length=200,
        description="Sent by callers as the X-Webhook-Secret header.",
    )

    @model_validator(mode="after")
    def _secret_when_required(self) -> WebhookTriggerConfig:
        # A switch with nothing behind it: validation is where that gets
        # caught, not the first 3am call that sails through unchecked.
        if self.require_signature and not self.secret.strip():
            raise ValueError("require_signature is on but no secret is set.")
        return self


class WebhookTriggerNode(Node):
    type = "trigger.webhook"
    label = "Webhook Trigger"
    description = "Start the flow from an inbound HTTP call."
    tier = 1
    category = "trigger"
    is_trigger = True
    config_model = WebhookTriggerConfig
    output_paths = ("body", "headers", "query", "method")

    async def run(self, config: WebhookTriggerConfig, ctx: NodeContext) -> NodeResult:
        payload = ctx.trigger.get("payload", {})
        return NodeResult(
            output={
                "body": payload.get("body"),
                "headers": payload.get("headers", {}),
                "query": payload.get("query", {}),
                "method": payload.get("method"),
            }
        )


class ScheduleTriggerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["interval", "cron"] = "interval"
    interval_seconds: int | None = Field(default=None, ge=30)
    cron: str | None = Field(default=None, max_length=120)
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def _five_fields(cls, value: str | None) -> str | None:
        # Shape only. Whether the expression is satisfiable is the scheduler's
        # problem, and it is not wired up in this phase.
        if value is not None and len(value.split()) != 5:
            raise ValueError("A cron expression needs five fields, e.g. '0 6 * * *'.")
        return value


class ScheduleTriggerNode(Node):
    type = "trigger.schedule"
    label = "Scheduler Trigger"
    description = "Run on a cron expression or a fixed interval."
    tier = 1
    category = "trigger"
    is_trigger = True
    config_model = ScheduleTriggerConfig
    output_paths = ("fired_at",)

    async def run(self, config: ScheduleTriggerConfig, ctx: NodeContext) -> NodeResult:
        return NodeResult(output={"fired_at": ctx.trigger.get("fired_at")})


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramTriggerConfig(BaseModel):
    """A bot's inbox.

    The secret is not configured here. Telegram will only ever send the value
    given to `setWebhook`, so the product generates one when the bot is
    connected and stores it beside the token — asking a studio owner to invent
    a shared secret and paste it into two places is how bots end up with no
    secret at all.
    """

    model_config = {"extra": "forbid"}

    credential_id: str = Field(
        default="",
        title="Bot",
        description="The Telegram credential holding this bot's token.",
    )
    #: Which updates are worth a run. A bot added to a group sees every
    #: message in it, and starting a flow for each one is someone's entire
    #: monthly compute spent on other people's conversation.
    accept: list[TelegramUpdateKind] = Field(
        default_factory=lambda: ["message", "photo", "document", "callback_query"],
        title="React to",
    )
    #: Chat ids allowed to use this bot, empty meaning anyone. A bot username
    #: is discoverable, and an open bot is an open invitation to spend your
    #: rendering budget.
    allowed_chats: list[str] = Field(
        default_factory=list,
        title="Only these chats",
        description="Telegram chat ids. Empty means anyone who finds the bot.",
    )
    #: Photos arrive as an id, not bytes. Fetching them here means the rest of
    #: the flow sees an artifact like any other, and a flow that only needs the
    #: caption does not pay for a download.
    download_media: bool = Field(default=True, title="Download photos")


class TelegramTriggerNode(Node):
    type = "trigger.telegram"
    label = "Telegram Bot"
    description = "Start when someone messages your bot. Photos arrive as files."
    tier = 1
    category = "trigger"
    is_trigger = True
    config_model = TelegramTriggerConfig
    output_paths = (
        "chat_id",
        "user",
        "text",
        "command",
        "photos",
        "callback_data",
        "message_id",
        "media_group_id",
        "kind",
    )

    async def run(self, config: TelegramTriggerConfig, ctx: NodeContext) -> NodeResult:
        """Normalise one update into something a flow author can read.

        Telegram's update object is a union of twenty optional keys, and the
        difference between a photo and a document and a compressed photo sent
        from a desktop is not something a studio owner should have to know.
        What comes out of here is flat: who, what they said, what they sent.
        """
        update = (ctx.trigger.get("payload") or {}).get("body") or {}
        normalised = normalise_update(update)

        if not normalised["chat_id"]:
            # A channel post, a poll answer, an edited message we do not
            # handle: end the run quietly rather than failing it. A red run for
            # every ignorable update makes the run list useless.
            await ctx.step("telegram.ignored", {"reason": "no chat in this update"})
            return NodeResult(output={**normalised, "ignored": True})

        if config.allowed_chats and normalised["chat_id"] not in config.allowed_chats:
            await ctx.step(
                "telegram.refused",
                {"chat_id": normalised["chat_id"], "reason": "not on the allowlist"},
            )
            return NodeResult(output={**normalised, "ignored": True, "refused": True})

        if config.accept and normalised["kind"] not in config.accept:
            await ctx.step("telegram.ignored", {"reason": f"{normalised['kind']} not accepted"})
            return NodeResult(output={**normalised, "ignored": True})

        if config.download_media and normalised["files"]:
            normalised["photos"] = await _download_files(config, normalised["files"], ctx)

        return NodeResult(output=normalised)


def normalise_update(update: dict[str, Any]) -> dict[str, Any]:
    """Telegram's twenty-key union, flattened.

    Pure, and separately testable, because the shapes are where the surprises
    live: an album is N updates sharing a `media_group_id`, a phone sends a
    photo as `photo` but a desktop can send the same picture as a `document`,
    and a button press is a `callback_query` whose message is the *bot's* last
    message rather than anything the person typed.
    """
    callback = update.get("callback_query") or {}
    message = callback.get("message") or update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    sender = callback.get("from") or message.get("from") or {}

    files: list[dict[str, Any]] = []
    kind = "message"
    if callback:
        kind = "callback_query"
    elif photo_sizes := message.get("photo"):
        # Telegram sends every thumbnail size; the last is the largest.
        largest = photo_sizes[-1]
        files.append(
            {
                "file_id": largest.get("file_id"),
                "file_unique_id": largest.get("file_unique_id"),
                "size": largest.get("file_size") or 0,
                "name": "photo.jpg",
            }
        )
        kind = "photo"
    elif document := message.get("document"):
        files.append(
            {
                "file_id": document.get("file_id"),
                "file_unique_id": document.get("file_unique_id"),
                "size": document.get("file_size") or 0,
                "name": document.get("file_name") or "file",
                "mime": document.get("mime_type") or "",
            }
        )
        kind = "document"
    elif audio := (message.get("audio") or message.get("voice")):
        files.append(
            {
                "file_id": audio.get("file_id"),
                "file_unique_id": audio.get("file_unique_id"),
                "size": audio.get("file_size") or 0,
                "name": audio.get("file_name") or "audio",
                "mime": audio.get("mime_type") or "",
            }
        )
        kind = "audio"

    text = (
        callback.get("data") if callback else (message.get("text") or message.get("caption") or "")
    )
    command = ""
    if not callback and isinstance(text, str) and text.startswith("/"):
        # "/generate@studio_bot extra words" → "/generate". A bot in a group is
        # addressed by name, and the name is not part of the command.
        command = text.split()[0].split("@")[0].lower()

    return {
        "kind": kind,
        "chat_id": str(chat.get("id")) if chat.get("id") is not None else "",
        "chat_type": chat.get("type") or "",
        "user": {
            "id": str(sender.get("id") or ""),
            "name": " ".join(
                part for part in (sender.get("first_name"), sender.get("last_name")) if part
            ),
            "username": sender.get("username") or "",
        },
        "text": "" if callback else (message.get("text") or message.get("caption") or ""),
        "command": command,
        "callback_data": callback.get("data") or "",
        "callback_id": callback.get("id") or "",
        "message_id": message.get("message_id"),
        # Every photo of an album carries the same one. The flow uses it to
        # know that eight updates are one act, not eight.
        "media_group_id": message.get("media_group_id") or "",
        "update_id": update.get("update_id"),
        "files": files,
        "photos": [],
        "ignored": False,
    }


#: Telegram will not serve a bot a file larger than this, so there is no point
#: asking. A phone photo is 2-5MB; this is the wall you hit sending a video.
MAX_TELEGRAM_DOWNLOAD_BYTES = 20 * 1024 * 1024


async def _download_files(
    config: TelegramTriggerConfig, files: list[dict[str, Any]], ctx: NodeContext
) -> list[dict[str, Any]]:
    """Turn Telegram file ids into artifacts the rest of the flow can use.

    Two calls per file: `getFile` for a path, then a GET on that path. The path
    contains the bot token, so it is never logged or returned — it is a
    credential in a URL, and it lands in the run log otherwise.
    """
    if not config.credential_id:
        raise NodeError(
            "This trigger has no bot credential, so it cannot fetch the photos "
            "someone sent. Pick the bot's credential on the trigger."
        )
    credential = await ctx.resolve_credential(config.credential_id)
    if credential is None:
        raise NodeError("That Telegram credential no longer exists in this workspace.")

    base = (credential.base_url or "https://api.telegram.org").rstrip("/")
    saved: list[dict[str, Any]] = []

    for item in files:
        if item.get("size", 0) > MAX_TELEGRAM_DOWNLOAD_BYTES:
            # Reported rather than raised: one oversized picture in an album
            # should cost that picture, not the whole job.
            await ctx.step(
                "telegram.file_too_large",
                {"name": item.get("name"), "bytes": item.get("size")},
            )
            continue

        lookup = await ctx.http.get(
            f"{base}/bot{credential.api_key}/getFile", params={"file_id": item["file_id"]}
        )
        payload = lookup.json() if lookup.content else {}
        if not payload.get("ok"):
            await ctx.step(
                "telegram.file_unavailable",
                {"name": item.get("name"), "reason": str(payload.get("description"))[:200]},
            )
            continue

        path = payload["result"].get("file_path", "")
        response = await ctx.http.get(f"{base}/file/bot{credential.api_key}/{path}")
        response.raise_for_status()

        record = await ctx.save_artifact(
            response.content,
            filename=item.get("name") or path.rsplit("/", 1)[-1] or "file",
            content_type=_content_type_for(path, item.get("mime")),
            node_id=ctx.node_id,
        )
        saved.append(
            {
                **record,
                # Telegram's stable identity for this exact file. The same
                # photo sent twice has the same one, which is how a session
                # collects eight photos rather than eight copies of one.
                "file_unique_id": item.get("file_unique_id", ""),
            }
        )

    await ctx.step("telegram.files_downloaded", {"count": len(saved)})
    return saved


def _content_type_for(path: str, declared: str | None) -> str:
    """What the bytes actually are.

    Telegram's own path extension is trusted over the sender's declared MIME
    type: the first comes from Telegram's storage, the second is whatever the
    client felt like claiming.
    """
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    known = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "heic": "image/heic",
        "mp4": "video/mp4",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "ogg": "audio/ogg",
    }
    return known.get(suffix) or declared or "application/octet-stream"
