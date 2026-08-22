"""The conversation's memory, between runs.

A run is one pass through a graph. A job — collect photos, generate, look at
it, change one thing, generate again — is a conversation that lasts hours and
survives deploys. This node is the join between the two: every run reads the
session at the start and writes it at the end, so the graph stays a DAG and the
loop lives in the data.

Why not agent memory, which already exists: that stores *turns of dialogue* for
a model to read. This stores the state of a job — which photos, which state,
how many attempts, who is holding the render lock. Different lifetime,
different consumer, and conflating them would mean a model's context window
deciding when a lock expires.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

SessionAction = Literal[
    "read", "update", "add_photo", "remove_photo", "clear_photos", "lock", "unlock", "forget"
]


class SessionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    action: SessionAction = Field(default="read", title="Action")
    chat_id: str = Field(default="{{ input.chat_id }}", title="Conversation")

    #: For `update`: only the fields set here are written, so two nodes editing
    #: different parts of the session cannot erase each other's work.
    state: str = Field(default="", title="Set state")
    brief: str = Field(default="", title="Set the brief")
    options: dict[str, Any] = Field(default_factory=dict, title="Set options")
    status_message_id: str = Field(default="", title="Set status message")
    last_video_artifact_id: str = Field(default="", title="Set last video")
    bump_iteration: bool = Field(default=False, title="Count another attempt")
    add_spend_usd: float = Field(default=0.0, ge=0, title="Add spend")

    #: For `add_photo`.
    artifact_id: str = Field(default="", title="Photo")
    file_unique_id: str = Field(default="", title="Telegram file id")
    caption: str = Field(default="", max_length=400, title="Caption")

    @model_validator(mode="after")
    def _needs_its_input(self) -> SessionConfig:
        if self.action == "add_photo" and not self.artifact_id.strip():
            raise ValueError("Adding a photo needs the artifact it came from.")
        return self


class SessionNode(Node):
    type = "session.state"
    label = "Conversation State"
    description = "Remember a chat's photos, brief and progress between messages."
    tier = 1
    category = "data"
    config_model = SessionConfig
    output_paths = (
        "chat_id",
        "state",
        "photos",
        "photo_count",
        "photo_ids",
        "brief",
        "options",
        "status_message_id",
        "last_video_artifact_id",
        "iteration",
        "spend_usd",
        "locked",
        "acquired",
        "added",
        "duplicate",
        "is_new",
    )

    async def run(self, config: SessionConfig, ctx: NodeContext) -> NodeResult:
        if ctx.session_state is None:
            raise NodeError("Conversation state is only available inside a real run.")

        # Rendered here rather than by the engine, which is this codebase's
        # convention: the node decides which of its fields accept a reference,
        # so a credential id can never be one.
        template = ctx.template_context()

        def rendered(value: str) -> str:
            return str(render_value(value, template)) if "{{" in value else value

        chat_id = rendered(config.chat_id).strip()
        if not chat_id:
            raise NodeError(
                "No conversation to remember. This is usually {{ input.chat_id }} "
                "from the Telegram trigger."
            )

        result = await ctx.session_state(
            chat_id=chat_id,
            action=config.action,
            fields={
                "state": rendered(config.state),
                "brief": rendered(config.brief),
                "options": render_value(config.options, template),
                "status_message_id": rendered(config.status_message_id),
                "last_video_artifact_id": rendered(config.last_video_artifact_id),
                "bump_iteration": config.bump_iteration,
                "add_spend_usd": config.add_spend_usd,
                "artifact_id": rendered(config.artifact_id),
                "file_unique_id": rendered(config.file_unique_id),
                "caption": rendered(config.caption),
            },
        )

        await ctx.step(
            "session.applied",
            {
                "action": config.action,
                "state": result.get("state"),
                "photos": result.get("photo_count"),
                "iteration": result.get("iteration"),
            },
        )
        return NodeResult(output=result)
