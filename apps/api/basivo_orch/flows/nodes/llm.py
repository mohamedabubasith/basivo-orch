"""One model call. No loop, no tools, no memory.

The Agent node is the right shape when a step needs judgement over several
turns. Most steps do not: a scheduled content pipeline that writes one
paragraph, hands it to the video node and posts the result never needs the
model to decide anything, and running it through an agent loop pays for a
graph, a tool binding and an extra round trip to get a paragraph back.

So this node is deliberately the smaller thing — render the prompt, invoke the
model once, record what it cost. Cost accounting is the same shape the Agent
node emits (`usage.cost_usd`), because the run detail page and the analysis
layer read one field, not one per node type.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.models import build_chat_model, price_of
from basivo_orch.flows.nodes.video import message_text_of, strip_code_fences
from basivo_orch.flows.templating import render_value

#: Appended to the system prompt when JSON is asked for. Kept short on purpose:
#: a long specification invites the model to explain the JSON it is about to
#: write, and the explanation is what breaks the parse.
JSON_INSTRUCTION = "Reply with a single JSON object and nothing else."


class LlmGenerateConfig(BaseModel):
    model_config = {"extra": "forbid"}

    prompt: str = Field(
        min_length=1,
        max_length=20000,
        title="Prompt",
        description="What to write. Supports {{ references }}, for example {{ input.text }}.",
    )
    system: str = Field(
        default="",
        max_length=20000,
        title="Instructions",
        description="How to write it: voice, length, audience. Supports {{ references }}.",
    )

    provider: str = Field(default="anthropic", max_length=48)
    model: str = Field(default="claude-sonnet-5", max_length=160)
    credential_id: str = Field(
        default="",
        title="Model credential",
        description="The saved key this node calls the model with.",
    )

    temperature: float = Field(
        default=0.7,
        ge=0,
        le=2,
        title="Temperature",
        description="Low keeps it predictable. High makes it varied.",
    )
    max_tokens: int = Field(
        default=800,
        ge=1,
        le=32000,
        title="Maximum length",
        description="The longest reply to allow, in tokens. Roughly four characters each.",
    )

    output: Literal["text", "json"] = Field(
        default="text",
        title="Output",
        description="Pick JSON object when a later node needs named fields out of the reply.",
        json_schema_extra={
            "x-enum-labels": {"text": "Plain text", "json": "JSON object"},
        },
    )


class LlmGenerateNode(Node):
    type = "llm.generate"
    label = "Write with AI"
    description = "One model call that writes text. No tools, no memory, no loop."
    when = (
        "The step only needs words written: a post, a summary, a subject line, a set of fields. "
        "Use AI Agent instead when the step has to call tools or remember earlier runs."
    )
    needs = (
        (
            "An LLM credential (OpenAI, Anthropic, Gemini, Groq or another provider) saved under "
            "Credentials"
        ),
        "A trigger before it, or any node whose output the prompt should write from",
    )
    example = "Schedule -> Write with AI -> Describe a Video -> Post to Social"
    tier = 2
    category = "ai"
    config_model = LlmGenerateConfig
    output_paths = (
        "text",
        "json",
        "usage.input_tokens",
        "usage.output_tokens",
        "usage.cost_usd",
    )

    max_attempts = 2
    retry_backoff_seconds = 2.0
    timeout_seconds = 180.0

    async def run(self, config: LlmGenerateConfig, ctx: NodeContext) -> NodeResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        template = ctx.template_context()
        prompt = render_value(config.prompt, template)
        if not isinstance(prompt, str):
            # A reference to a whole upstream object renders as a dict. Sending
            # its repr would put Python quoting in the prompt, so it goes as
            # JSON, which every model reads.
            prompt = json.dumps(prompt, default=str)
        if not prompt.strip():
            raise NodeError(
                "The prompt rendered empty, so there is nothing to write from. Check the "
                "reference in the prompt against what the node before this one returns."
            )

        system = str(render_value(config.system, template)) if config.system else ""
        if config.output == "json":
            system = f"{system}\n\n{JSON_INSTRUCTION}".strip()

        model = await build_chat_model(
            ctx,
            provider=config.provider,
            model=config.model,
            credential_id=config.credential_id,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        await ctx.progress(f"Writing with {config.model}")
        conversation: list[Any] = [HumanMessage(content=prompt)]
        if system:
            conversation.insert(0, SystemMessage(content=system))

        reply = await model.ainvoke(conversation)
        text = message_text_of(reply).strip()

        usage = getattr(reply, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = price_of(
            model=config.model,
            provider=config.provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # The whole reply, not a preview: when the JSON parse below fails, this
        # step is the only place a person can see what the model actually said,
        # and a 400-character preview of a malformed object usually cuts off
        # before the part that went wrong.
        await ctx.step(
            "llm.response",
            {
                "provider": config.provider,
                "model": config.model,
                "output": config.output,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost, 6) if cost is not None else None,
                "text": text[:8000],
            },
        )

        if not text:
            raise NodeError(
                "The model replied with nothing. Give the prompt something concrete to write "
                "about, or raise the maximum length if the reply is being cut off."
            )

        parsed = _as_json(text) if config.output == "json" else None

        return NodeResult(
            output={
                "text": text,
                "json": parsed,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": round(cost or 0.0, 6),
                },
            },
            metrics={
                "tokens_in": input_tokens,
                "tokens_out": output_tokens,
                "cost_usd": round(cost or 0.0, 6),
            },
        )


def _as_json(text: str) -> Any:
    """The reply as an object, or a failure someone can act on.

    Models fence their JSON perhaps one time in five even when told not to, and
    a run that dies on a stray ``` is a support ticket rather than a bug. Prose
    is different: a node set to JSON whose reply is prose has failed at the one
    thing it was asked for, and passing null down to a node templating
    `{{ input.json.title }}` would break somewhere much further from the cause.
    """
    try:
        return json.loads(strip_code_fences(text))
    except ValueError:
        raise NodeError(
            "This node is set to return a JSON object and the model replied with something "
            "else. Tell it in the instructions exactly which fields to return, or set Output "
            f"back to plain text. It said: {text[:300]}"
        ) from None
