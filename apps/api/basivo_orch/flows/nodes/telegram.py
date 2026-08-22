"""Talking back.

Everything the bot says to a person goes through this node, and it exists as
one node with an `action` rather than five nodes because the flow author is
building a conversation, not a REST integration — "say this", "change what you
said", "send the video" belong on one card.

Three behaviours here are not obvious and are the difference between a bot that
feels finished and one that feels like a script:

**It edits one message instead of sending twenty.** A pipeline that posts
"enhancing…", "directing…", "rendering…" leaves the operator scrolling through
its inner monologue. One message that changes reads like progress.

**It escapes what people typed.** Telegram's HTML parse mode rejects a message
containing a bare `<`, and a studio that names a file "before<>after" would get
a bot that silently stops replying. Their text is escaped; ours is not.

**It obeys 429.** Telegram answers a flood with `retry_after` in seconds, and a
client that ignores it gets the bot limited harder. Sending an album's worth of
replies is exactly how you find that out in production.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

TelegramAction = Literal["send", "edit", "video", "photo", "typing", "answer_callback", "delete"]

#: Telegram's own limits. Exceeding them is a 400 with a message nobody reads.
MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024
#: The Bot API refuses an upload larger than this. A 30 second 1080p montage is
#: around 15MB, so this is the ceiling on how long a delivered video can be.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: One retry, and only for a flood wait. Retrying a 400 just sends the same
#: broken message again.
MAX_FLOOD_RETRIES = 1


class Button(BaseModel):
    model_config = {"extra": "forbid"}

    label: str = Field(min_length=1, max_length=64, title="Label")
    #: Comes back as `callback_data` on the next update. Keep it short and
    #: parseable — Telegram caps it at 64 *bytes*, and an emoji is four.
    data: str = Field(min_length=1, max_length=64, title="Sends back")


class TelegramReplyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    credential_id: str = Field(default="", title="Bot")
    action: TelegramAction = Field(default="send", title="Action")
    chat_id: str = Field(
        default="{{ input.chat_id }}",
        title="Chat",
        description="Usually left as the chat the message came from.",
    )
    text: str = Field(default="", max_length=4096, title="Text")
    #: Which message to change or delete. The flow keeps this from whichever
    #: reply created it, which is what makes a status line possible.
    message_id: str = Field(default="", title="Message to edit")
    #: An artifact from an earlier node — the rendered video, a poster.
    artifact_id: str = Field(default="", title="File to send")
    buttons: list[list[Button]] = Field(
        default_factory=list,
        title="Buttons",
        description="Rows of buttons under the message.",
    )
    #: Answering a button press within a few seconds is what stops the spinner
    #: on the operator's phone. Telegram requires it whether or not you have
    #: anything to say.
    callback_id: str = Field(default="{{ input.callback_id }}", title="Button press id")
    silent: bool = Field(default=False, title="No notification")

    @model_validator(mode="after")
    def _needs_its_input(self) -> TelegramReplyConfig:
        if self.action in {"send", "edit"} and not self.text.strip():
            raise ValueError(f"A '{self.action}' needs some text.")
        if self.action in {"video", "photo"} and not self.artifact_id.strip():
            raise ValueError(f"A '{self.action}' needs a file to send.")
        if self.action in {"edit", "delete"} and not self.message_id.strip():
            raise ValueError(f"A '{self.action}' needs the id of the message to change.")
        return self


#: Which fields each action actually reads. Rendering the others would fail a
#: perfectly good `send` because the unused `callback_id` default — which only
#: means anything after a button press — has nothing to resolve against.
FIELDS_USED_BY: dict[str, tuple[str, ...]] = {
    "send": ("chat_id", "text"),
    "edit": ("chat_id", "text", "message_id"),
    "video": ("chat_id", "text", "artifact_id"),
    "photo": ("chat_id", "text", "artifact_id"),
    "typing": ("chat_id",),
    "answer_callback": ("callback_id", "text"),
    "delete": ("chat_id", "message_id"),
}


class TelegramReplyNode(Node):
    type = "telegram.reply"
    label = "Telegram Reply"
    description = "Send, edit or delete a message, or send a rendered file."
    tier = 1
    category = "social"
    config_model = TelegramReplyConfig
    output_paths = ("message_id", "chat_id", "sent")

    max_attempts = 2
    retry_backoff_seconds = 2.0

    async def run(self, config: TelegramReplyConfig, ctx: NodeContext) -> NodeResult:
        # Every field except the credential accepts a reference. The credential
        # deliberately does not: a bot token chosen at run time by upstream data
        # is a bot token chosen by whoever controls that data.
        template = ctx.template_context()
        config = config.model_copy(
            update={
                field: str(render_value(getattr(config, field), template))
                for field in FIELDS_USED_BY[config.action]
                if "{{" in getattr(config, field)
            }
        )

        credential = await ctx.resolve_credential(config.credential_id)
        if credential is None:
            raise NodeError(
                "Pick the bot's Telegram credential on this node. Without it there "
                "is nothing to send the message as."
            )
        if not config.chat_id.strip():
            raise NodeError(
                "No chat to reply to. This is usually {{ input.chat_id }} from the "
                "Telegram trigger."
            )

        base = (credential.base_url or "https://api.telegram.org").rstrip("/")
        api = f"{base}/bot{credential.api_key}"

        method, data, files = await self._compose(config, ctx)
        payload = await self._call(ctx, api, method, data, files)

        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        await ctx.step(
            "telegram.sent",
            {"action": config.action, "chat_id": config.chat_id, "message_id": message_id},
        )
        return NodeResult(
            output={
                "message_id": message_id,
                "chat_id": config.chat_id,
                "sent": True,
            }
        )

    async def _compose(
        self, config: TelegramReplyConfig, ctx: NodeContext
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Which Bot API method, and what to send it."""
        common: dict[str, Any] = {"chat_id": config.chat_id}
        if config.buttons:
            # Telegram wants this as a JSON *string* inside form data, which is
            # the kind of detail that costs an hour when it is wrong.
            import json

            common["reply_markup"] = json.dumps(
                {
                    "inline_keyboard": [
                        [{"text": button.label, "callback_data": button.data} for button in row]
                        for row in config.buttons
                    ]
                }
            )

        if config.action == "typing":
            # Not cosmetic: a render takes minutes, and the typing indicator is
            # the only thing Telegram offers that says "still alive" without
            # another message.
            return "sendChatAction", {**common, "action": "upload_video"}, None

        if config.action == "answer_callback":
            if not config.callback_id.strip():
                raise NodeError(
                    "This action answers a button press, so it needs the press id: "
                    "{{ input.callback_id }} from the trigger."
                )
            return (
                "answerCallbackQuery",
                {"callback_query_id": config.callback_id, "text": config.text[:200]},
                None,
            )

        if config.action == "delete":
            return "deleteMessage", {**common, "message_id": config.message_id}, None

        if config.action in {"video", "photo"}:
            data = await self._load_file(config, ctx)
            caption = _clip(config.text, MAX_CAPTION_CHARS)
            body = {
                **common,
                "caption": caption,
                "parse_mode": "HTML",
                "disable_notification": config.silent,
            }
            if config.action == "video":
                body["supports_streaming"] = True
                return "sendVideo", body, {"video": ("video.mp4", data, "video/mp4")}
            return "sendPhoto", body, {"photo": ("image.png", data, "image/png")}

        text = _clip(config.text, MAX_MESSAGE_CHARS)
        if config.action == "edit":
            return (
                "editMessageText",
                {**common, "message_id": config.message_id, "text": text, "parse_mode": "HTML"},
                None,
            )
        return (
            "sendMessage",
            {**common, "text": text, "parse_mode": "HTML", "disable_notification": config.silent},
            None,
        )

    async def _load_file(self, config: TelegramReplyConfig, ctx: NodeContext) -> bytes:
        try:
            artifact_id = uuid.UUID(config.artifact_id.strip())
        except ValueError:
            raise NodeError(
                f"{config.artifact_id!r} is not a file reference. This is usually "
                "{{ nodes.render.artifact_id }} from the node that made the video."
            ) from None

        data = await ctx.load_artifact(artifact_id)
        if data is None:
            raise NodeError(
                "That file is no longer stored. Rendered files are kept for a limited "
                "time, so a run replayed days later has nothing to send."
            )
        if len(data) > MAX_UPLOAD_BYTES:
            raise NodeError(
                f"Telegram refuses uploads over {MAX_UPLOAD_BYTES // (1024 * 1024)}MB and "
                f"this is {len(data) // (1024 * 1024)}MB. Shorten the video or lower "
                "the resolution."
            )
        return data

    async def _call(
        self,
        ctx: NodeContext,
        api: str,
        method: str,
        data: dict[str, Any],
        files: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """One Bot API call, with the flood wait honoured."""
        for attempt in range(MAX_FLOOD_RETRIES + 1):
            response = await ctx.http.post(f"{api}/{method}", data=data, files=files)
            payload = response.json() if response.content else {}

            if payload.get("ok"):
                return payload

            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if response.status_code == 429 and retry_after and attempt < MAX_FLOOD_RETRIES:
                await ctx.progress(f"Telegram asked us to wait {retry_after}s")
                await asyncio.sleep(min(float(retry_after), 30.0))
                continue

            description = str(payload.get("description") or response.text)[:300]
            raise NodeError(_explain(method, response.status_code, description))

        raise NodeError("Telegram kept asking us to wait. Try again in a minute.")


def _clip(text: str, limit: int) -> str:
    """Trim to Telegram's limit without cutting an HTML tag in half."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit("<", 1)[0].rstrip() + "…"


def escape(text: str) -> str:
    """Anything a person typed, before it goes into an HTML-parsed message."""
    return html.escape(text, quote=False)


def _explain(method: str, status: int, description: str) -> str:
    """Telegram's errors are terse and the causes are well known."""
    lowered = description.lower()
    if "bot was blocked" in lowered:
        return "That person has blocked this bot, so it cannot message them."
    if "chat not found" in lowered:
        return (
            "Telegram does not know that chat. A bot can only message someone who "
            "has messaged it first, and a group id is negative."
        )
    if "message is not modified" in lowered:
        return (
            "The status message already says exactly this. Telegram treats an edit "
            "with identical text as an error."
        )
    if "message to edit not found" in lowered:
        return "The message being edited was deleted, so there is nothing to change."
    if "can't parse entities" in lowered:
        return (
            "Telegram rejected the message formatting. Text that came from a person "
            "needs escaping before it goes into an HTML message."
        )
    return f"Telegram refused {method} ({status}): {description}"
