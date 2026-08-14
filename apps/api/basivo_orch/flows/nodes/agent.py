"""The Agent node — a model with tools, in a loop, fully instrumented.

This is the node the product exists for. Every other orchestrator can call an
LLM; the question this one has to answer is *what happened inside that call* —
how many turns, which tools fired with what arguments, what each one returned,
how many tokens each turn consumed, and what it cost. So the loop below emits a
structured step at every boundary rather than logging a summary at the end.

Four decisions worth stating.

**Provider execution is pydantic-ai's job, not ours.** It ships correct,
maintained clients for every major provider — see `PROVIDER_MODEL_CLASS` below
— including each provider's particular tool-call wire format, retries and
streaming. Reimplementing that per provider is where hand-rolled agent nodes
accumulate bugs; this node stays a thin, observed shell around it. Cost is not
computed here either: pydantic-ai prices every response automatically via
`genai-prices`, keyed on the provider and model name, so the number updates
when a provider changes pricing rather than drifting from a hand-maintained
table.

**Steps are events, not a summary field.** An agent execution is a sequence,
and a sequence flattened into one row loses the order and the per-turn cost.
Steps go to `run_event`, which already has a gapless per-run sequence and
replay, so the detail survives a dropped connection and can be read back long
after the run finished. `agent.iter()` is used instead of `agent.run()`
specifically so each model turn is observed as it happens.

**Tools are declared, not arbitrary code.** A tool here is a JSON Schema plus
one of two bodies: an HTTP call, or a constant. Letting a flow author attach
executable code to a model that chooses when to run it is a remote-code-
execution feature with extra steps. HTTP tools go through the same SSRF guard
as the HTTP node. `Tool.from_schema` builds a pydantic-ai tool straight from
that JSON Schema, so there is no second, hand-written schema to drift from the
one the model sees, and the wrapper function is where `tool.called` /
`tool.result` are actually logged — it is the one place execution, timing and
outcome are all in scope together.

**Credentials are resolved by the engine, never embedded in the graph.** The
node's config carries a `credential_id`, not a key. `NodeContext.resolve_credential`
(see `base.py`) is how the node reaches it — implemented by the engine, which
holds the database session; the node itself never touches SQL. The secret is
decrypted for the duration of one call and goes nowhere else, in particular
never into `NodeExecution.input_summary`.
"""

from __future__ import annotations

import importlib
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from basivo_orch.credentials.provider_client import construct_provider
from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.http import assert_public_url
from basivo_orch.flows.templating import render_value

#: Which Model class a provider's responses are shaped like. Most providers
#: speak an OpenAI-compatible wire format even though they are not OpenAI, so
#: several names below share `OpenAIChatModel` with their own `Provider`
#: subclass supplying the base URL and auth header. This mirrors
#: `pydantic_ai.models.infer_model`'s own routing table; kept explicit here
#: rather than calling that function because it constructs its provider from
#: environment variables, and this node must inject a *stored* credential
#: instead.
PROVIDER_MODEL_MODULE: dict[str, tuple[str, str]] = {
    "anthropic": ("pydantic_ai.models.anthropic", "AnthropicModel"),
    "openai": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "azure": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "deepseek": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "moonshotai": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "together": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "fireworks": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "google": ("pydantic_ai.models.google", "GoogleModel"),
    "groq": ("pydantic_ai.models.groq", "GroqModel"),
    "mistral": ("pydantic_ai.models.mistral", "MistralModel"),
    "cohere": ("pydantic_ai.models.cohere", "CohereModel"),
    "bedrock": ("pydantic_ai.models.bedrock", "BedrockConverseModel"),
    "huggingface": ("pydantic_ai.models.huggingface", "HuggingFaceModel"),
    "openrouter": ("pydantic_ai.models.openrouter", "OpenRouterModel"),
    "cerebras": ("pydantic_ai.models.cerebras", "CerebrasModel"),
    "ollama": ("pydantic_ai.models.ollama", "OllamaModel"),
    "zai": ("pydantic_ai.models.zai", "ZaiModel"),
    "xai": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "sambanova": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "nebius": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "ovhcloud": ("pydantic_ai.models.openai", "OpenAIChatModel"),
    "alibaba": ("pydantic_ai.models.openai", "OpenAIChatModel"),
}


class ToolDefinition(BaseModel):
    """One tool the model may call.

    `input_schema` is handed to the model verbatim via `Tool.from_schema`, so
    the model's idea of the arguments and the executor's are the same object.
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(default="", max_length=1024)
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description="JSON Schema for the arguments. Sent to the model as-is.",
    )

    kind: Literal["http", "constant"] = "http"

    # -- http tools ----------------------------------------------------------
    url: str = Field(default="", description="Supports {{ references }} and {{ tool.<arg> }}.")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    #: Omit to send the model's arguments as the JSON body unchanged.
    body: Any = None
    timeout_seconds: float = Field(default=30.0, ge=1, le=120)

    # -- constant tools --------------------------------------------------------
    value: Any = Field(default=None, description="Returned verbatim. For stubbing a tool.")


class AgentConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # -- model -----------------------------------------------------------------
    #: One of PROVIDER_MODEL_MODULE's keys — every provider pydantic-ai
    #: supports with the SDKs this deployment has installed.
    provider: str = Field(default="anthropic", max_length=48)
    model: str = Field(default="claude-sonnet-5", max_length=160)
    #: A saved credential's id (see `basivo_orch/credentials/`). Leave empty to
    #: fall back to that provider SDK's own environment-variable lookup.
    credential_id: str = Field(default="", description="A saved credential's id.")

    system: str = Field(default="", max_length=20000, description="Supports {{ references }}.")
    prompt: str = Field(
        default="{{ input }}",
        max_length=20000,
        description="The user message. Supports {{ references }}.",
    )

    # -- sampling ------------------------------------------------------------
    max_tokens: int = Field(default=2048, ge=1, le=64000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = Field(default=None)
    stop_sequences: list[str] = Field(default_factory=list, max_length=8)

    # -- tools ---------------------------------------------------------------
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=32)
    #: How many model turns the loop may take. Each round of tool results costs
    #: a turn, so an agent that keeps calling tools is bounded, not unbounded.
    max_iterations: int = Field(default=6, ge=1, le=25)
    #: A hard ceiling independent of turn count — the number that actually
    #: bounds spend when a single turn calls several tools at once.
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    cost_limit_usd: float | None = Field(
        default=None, ge=0, description="Abort the run if this is exceeded mid-loop."
    )

    # -- output ----------------------------------------------------------------
    response_format: Literal["text", "json"] = "text"

    # -- transport -------------------------------------------------------------
    base_url: str = Field(default="", max_length=300, description="Overrides the credential's.")
    request_timeout_seconds: float = Field(default=120.0, ge=5, le=600)


class AgentNode(Node):
    type = "agent.llm"
    label = "AI Agent"
    description = (
        "A model with tools, in a loop, on any major provider. Every turn, "
        "tool call, token count and cost is recorded as its own step."
    )
    tier = 2
    category = "ai"
    config_model = AgentConfig

    max_attempts = 2
    retry_backoff_seconds = 2.0
    #: Generous: the ceiling is the loop (`max_iterations` / `max_tool_calls`),
    #: and a slow tool should not be killed mid-call by a timeout tuned for a
    #: single HTTP request.
    timeout_seconds = 660.0

    async def run(self, config: AgentConfig, ctx: NodeContext) -> NodeResult:
        model = await _build_model(config, ctx)

        template = ctx.template_context()
        system = str(render_value(config.system, template)) if config.system else ""
        prompt = render_value(config.prompt, template)
        if not isinstance(prompt, str):
            prompt = json.dumps(prompt, default=str)
        if not prompt.strip():
            raise NodeError("The prompt rendered empty — there is nothing to send.")

        if config.response_format == "json":
            # PromptedOutput/NativeOutput want a concrete schema to validate
            # against; "any JSON object the model chooses to return" is not
            # one. Plain text output plus a best-effort parse (`_parse_json`)
            # is the more honest fit — and it degrades to a clear NodeError
            # instead of an opaque `UnexpectedModelBehavior` from a validator
            # that was never going to accept an open-ended shape.
            system = (f"{system}\n\nRespond with a single JSON object and nothing else.").strip()

        agent: Agent[None, Any] = Agent(
            model,
            instructions=system or None,
            tools=_build_tools(config.tools, ctx, template),
            model_settings=_model_settings(config),
        )

        limits = UsageLimits(
            request_limit=config.max_iterations,
            tool_calls_limit=config.max_tool_calls,
            cost_limit=config.cost_limit_usd,
        )

        await ctx.step(
            "agent.started",
            {
                "provider": config.provider,
                "model": config.model,
                "tools": [tool.name for tool in config.tools],
                "max_iterations": config.max_iterations,
            },
        )

        text = ""
        stop_reason = "end_turn"
        turn_started = time.perf_counter()

        try:
            async with agent.iter(prompt, usage_limits=limits) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        turn_started = time.perf_counter()

                    elif Agent.is_call_tools_node(node):
                        response = node.model_response
                        elapsed = int((time.perf_counter() - turn_started) * 1000)
                        usage = response.usage
                        tool_call_parts = [
                            part for part in response.parts if isinstance(part, ToolCallPart)
                        ]
                        text_preview = "".join(
                            getattr(part, "content", "") or ""
                            for part in response.parts
                            if getattr(part, "part_kind", None) == "text"
                        )
                        await ctx.step(
                            "llm.response",
                            {
                                "model": response.model_name,
                                "provider": response.provider_name,
                                "finish_reason": response.finish_reason,
                                "duration_ms": elapsed,
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "cache_read_tokens": usage.cache_read_tokens,
                                "cache_write_tokens": usage.cache_write_tokens,
                                "tool_calls": [part.tool_name for part in tool_call_parts],
                                "text_preview": text_preview[:400],
                            },
                        )

                if run.result is not None:
                    text = run.result.output
        except UsageLimitExceeded as exc:
            stop_reason = "limit_exceeded"
            await ctx.step("agent.truncated", {"reason": str(exc)})
        except (ModelHTTPError, UnexpectedModelBehavior) as exc:
            raise NodeError(f"The model provider returned an error: {exc}", retryable=True) from exc

        total_usage = run.usage
        cost = float(total_usage.cost) if total_usage.cost is not None else 0.0

        json_output = _parse_json(text) if config.response_format == "json" else None

        await ctx.step(
            "agent.finished",
            {
                "stop_reason": stop_reason,
                "tool_calls": total_usage.tool_calls,
                "requests": total_usage.requests,
                "input_tokens": total_usage.input_tokens,
                "output_tokens": total_usage.output_tokens,
                "cost_usd": cost,
            },
        )

        return NodeResult(
            output={
                "text": text,
                "json": json_output,
                "stop_reason": stop_reason,
                "tool_calls": total_usage.tool_calls,
                "usage": {
                    "input_tokens": total_usage.input_tokens,
                    "output_tokens": total_usage.output_tokens,
                    "cost_usd": cost,
                },
            },
            metrics={
                "tokens_in": total_usage.input_tokens,
                "tokens_out": total_usage.output_tokens,
                "cost_usd": cost,
            },
        )


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


async def _build_model(config: AgentConfig, ctx: NodeContext) -> Model:
    if config.provider not in PROVIDER_MODEL_MODULE:
        raise NodeError(
            f"Unknown provider {config.provider!r}. See the node's Provider field for the list.",
            retryable=False,
        )

    api_key = ""
    base_url = config.base_url
    options: dict[str, Any] = {}

    if config.credential_id:
        credential = await ctx.resolve_credential(config.credential_id)
        if credential is None:
            raise NodeError(
                f"Credential {config.credential_id!r} was not found in this workspace.",
                retryable=False,
            )
        if credential.provider != config.provider:
            raise NodeError(
                f"This credential is for {credential.provider!r}, not {config.provider!r}.",
                retryable=False,
            )
        api_key = credential.api_key
        base_url = base_url or credential.base_url or ""
        options = credential.options

    provider_cls = infer_provider_class(config.provider)
    provider = construct_provider(provider_cls, api_key=api_key, base_url=base_url, options=options)

    module_name, class_name = PROVIDER_MODEL_MODULE[config.provider]

    model_cls = getattr(importlib.import_module(module_name), class_name)
    return model_cls(config.model, provider=provider)


# ---------------------------------------------------------------------------
# Sampling settings
# ---------------------------------------------------------------------------


def _model_settings(config: AgentConfig) -> ModelSettings:
    settings: ModelSettings = {
        "max_tokens": config.max_tokens,
        "timeout": config.request_timeout_seconds,
    }
    if config.temperature is not None:
        settings["temperature"] = config.temperature
    if config.top_p is not None:
        settings["top_p"] = config.top_p
    if config.seed is not None:
        settings["seed"] = config.seed
    if config.stop_sequences:
        settings["stop_sequences"] = config.stop_sequences
    return settings


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _build_tools(
    definitions: list[ToolDefinition], ctx: NodeContext, template: dict[str, Any]
) -> list[Tool[None]]:
    return [_make_tool(definition, ctx, template) for definition in definitions]


def _make_tool(
    definition: ToolDefinition, ctx: NodeContext, template: dict[str, Any]
) -> Tool[None]:
    async def call(**arguments: Any) -> Any:
        # The one place execution, timing and outcome are all in scope
        # together — logged here rather than reconstructed from pydantic-ai's
        # internal message history after the fact.
        await ctx.step(
            "tool.called",
            {"tool": definition.name, "kind": definition.kind, "arguments": arguments},
        )
        started = time.perf_counter()
        ok, output = await _execute_tool(definition, arguments, ctx, template)
        elapsed = int((time.perf_counter() - started) * 1000)
        await ctx.step(
            "tool.result",
            {
                "tool": definition.name,
                "ok": ok,
                "duration_ms": elapsed,
                "result_preview": str(output)[:400],
            },
        )
        return output

    return Tool.from_schema(
        call,
        name=definition.name,
        description=definition.description or None,
        json_schema=definition.input_schema,
        takes_ctx=False,
    )


async def _execute_tool(
    definition: ToolDefinition,
    arguments: dict[str, Any],
    ctx: NodeContext,
    template: dict[str, Any],
) -> tuple[bool, Any]:
    """Run one tool call. Never raises — a failed tool is a result the model
    sees and can adapt to, not a crashed run."""
    if definition.kind == "constant":
        return True, definition.value

    context = {**template, "tool": arguments}
    try:
        url = str(render_value(definition.url, context))
        if not url:
            return False, "This tool has no URL configured."
        # The same guard the HTTP node uses. An agent choosing its own
        # arguments is precisely the case where SSRF matters most: the URL is
        # now partly attacker-influenced through the prompt.
        assert_public_url(url)

        body = render_value(definition.body, context) if definition.body is not None else arguments
        headers = {str(k): str(v) for k, v in render_value(definition.headers, context).items()}

        kwargs: dict[str, Any] = {"headers": headers, "timeout": definition.timeout_seconds}
        if definition.method == "GET":
            if isinstance(body, dict):
                kwargs["params"] = body
        else:
            kwargs["json"] = body

        response = await ctx.http.request(definition.method, url, **kwargs)
        if response.status_code >= 400:
            return False, f"{response.status_code}: {response.text[:300]}"
        try:
            return True, response.json()
        except ValueError:
            return True, response.text[:4000]
    except NodeError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
        return False, f"{type(exc).__name__}: {exc}"


def _parse_json(text: str) -> Any:
    """Best-effort JSON out of a model response.

    Models fence JSON in markdown often enough that failing on it would make
    `response_format: json` unreliable for reasons that have nothing to do
    with the flow author's prompt.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate[3:]
        if candidate.lstrip().startswith("json"):
            candidate = candidate.lstrip()[4:]
        candidate = candidate.rsplit("```", 1)[0]
    try:
        return json.loads(candidate.strip())
    except ValueError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                pass
    raise NodeError("The model did not return valid JSON, but the node requires it.")
