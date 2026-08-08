"""How a flow starts.

Triggers do no work. They exist so the graph has a single, explicit entry point
and so the run log records what set the flow off — which is the difference
between "this failed" and "this failed every time the 6am schedule fired".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeResult

WebhookMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
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
    #: When set, the inbound request must carry this shared secret. Checked at
    #: the edge, before a run is created, so an unauthenticated caller cannot
    #: fill the run table by spraying the endpoint.
    require_signature: bool = False


class WebhookTriggerNode(Node):
    type = "trigger.webhook"
    label = "Webhook Trigger"
    description = "Start the flow from an inbound HTTP call."
    tier = 1
    category = "trigger"
    is_trigger = True
    config_model = WebhookTriggerConfig

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

    async def run(self, config: ScheduleTriggerConfig, ctx: NodeContext) -> NodeResult:
        return NodeResult(output={"fired_at": ctx.trigger.get("fired_at")})
