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

**Tools are a declared schema with one of three bodies.** An HTTP call, a
constant, or the author's own Python function — `def main(data)` with the
model's arguments at `data["args"]`. Code bodies run in the Code node's
sandboxed interpreter (`run_python`: isolated mode, CPU rlimit, wall timeout,
empty environment) under the Code node's stated trust model — flow authors are
authenticated workspace members, and this is containment against accidents,
not a jail; see `code.py`'s docstring for the full statement. HTTP tools go
through the same SSRF guard as the HTTP node, which matters most here: an
agent choosing its own arguments is a URL partly authored by the prompt.
`Tool.from_schema` builds a pydantic-ai tool straight from the JSON Schema, so
there is no second, hand-written schema to drift from the one the model sees,
and the wrapper function is where `tool.called` / `tool.result` are logged —
the one place execution, timing and outcome are all in scope together.

**Credentials are resolved by the engine, never embedded in the graph.** The
node's config carries a `credential_id`, not a key. `NodeContext.resolve_credential`
(see `base.py`) is how the node reaches it — implemented by the engine, which
holds the database session; the node itself never touches SQL. The secret is
decrypted for the duration of one call and goes nowhere else, in particular
never into `NodeExecution.input_summary`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.agent_runtime import (
    RunTotals,
    TeamMember,
    build_tool,
    delegation_tool,
    parse_json,
    run_agent,
    run_team,
)
from basivo_orch.flows.nodes.base import (
    DEFAULT_PORT,
    HANDOVER_PORT,
    Node,
    NodeContext,
    NodeError,
    NodeResult,
)
from basivo_orch.flows.nodes.code import PythonExecutionError, run_python
from basivo_orch.flows.nodes.http import assert_public_url
from basivo_orch.flows.nodes.models import build_chat_model
from basivo_orch.flows.nodes.skills import (
    LoadedSkill,
    SkillBudget,
    skill_tools,
)
from basivo_orch.flows.nodes.skills import (
    catalogue as skill_catalogue,
)
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

    kind: Literal["code", "http", "constant"] = "http"

    # -- code tools --------------------------------------------------------
    #: The user's own function — `def main(data)` with the model's arguments
    #: at data["args"] and the flow context (input/nodes/vars/trigger) beside
    #: them. Runs in the same sandboxed interpreter as the Code node: one
    #: sandbox, one contract, one place to harden.
    code: str = Field(default="", max_length=50_000)

    # -- http tools ----------------------------------------------------------
    url: str = Field(default="", description="Supports {{ references }} and {{ tool.<arg> }}.")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    #: Omit to send the model's arguments as the JSON body unchanged.
    body: Any = None
    timeout_seconds: float = Field(default=30.0, ge=1, le=120)

    # -- constant tools --------------------------------------------------------
    value: Any = Field(default=None, description="Returned verbatim. For stubbing a tool.")

    @model_validator(mode="after")
    def _body_matches_kind(self) -> ToolDefinition:
        # The webhook switch taught this lesson once: config that promises
        # behaviour with nothing behind it must fail at validation, not at the
        # first 3am call.
        if self.kind == "code" and not self.code.strip():
            raise ValueError(f"Tool {self.name!r} is a code tool with no code.")
        if self.kind == "http" and not self.url.strip():
            raise ValueError(f"Tool {self.name!r} is an HTTP tool with no URL.")
        return self


class SubAgentDefinition(BaseModel):
    """Another agent this one may hand work to.

    Agent-to-agent, made configurable: the parent gets an `ask_<name>` tool
    per entry, calling it runs this agent on the task, and its answer comes
    back as the tool result. A supervisor delegating to specialists, rather
    than free-form handoff — one agent stays in charge, the conversation
    terminates, and every hand-off is a step on the run log with its own cost.

    Wiring two Agent NODES together on the canvas is still the right shape for
    a fixed pipeline (writer → reviewer). This is for when the parent should
    decide, at run time, who to ask.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(
        min_length=1,
        max_length=48,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="How the parent refers to it, e.g. 'researcher'.",
    )
    description: str = Field(
        default="",
        max_length=600,
        description="What it is good at. The parent reads this to decide when to ask.",
    )
    system: str = Field(default="", max_length=20000, description="Its own instructions.")
    provider: str = Field(default="openai", max_length=48)
    model: str = Field(default="", max_length=160, description="Blank uses the parent's model.")
    credential_id: str = Field(default="", description="Blank uses the parent's credential.")
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=64000)
    max_iterations: int = Field(default=4, ge=1, le=15)


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
        description=(
            "The user message. Supports {{ references }}. Chain agents by "
            "using {{ input.text }}, the upstream agent's reply."
        ),
    )

    # -- sampling ------------------------------------------------------------
    max_tokens: int = Field(default=2048, ge=1, le=64000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = Field(default=None)
    stop_sequences: list[str] = Field(default_factory=list, max_length=8)

    # -- tools ---------------------------------------------------------------
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=32)
    #: Other agents this one may work with at run time.
    sub_agents: list[SubAgentDefinition] = Field(default_factory=list, max_length=8)
    #: How this agent works with them, when there are any.
    #:
    #: "delegate" — it asks (`ask_<name>`), receives an answer, and writes the
    #: reply itself. Right when several answers must be combined.
    #:
    #: "handover" — it transfers (`transfer_to_<name>`) and the receiving
    #: agent answers directly, and may transfer on again. Right for triage,
    #: where claiming the first agent wrote the final answer would be a lie.
    team_mode: Literal["delegate", "handover"] = "delegate"
    #: How many model turns the loop may take. Each round of tool results costs
    #: a turn, so an agent that keeps calling tools is bounded, not unbounded.
    max_iterations: int = Field(default=6, ge=1, le=25)
    #: A hard ceiling independent of turn count — the number that actually
    #: bounds spend when a single turn calls several tools at once.
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    cost_limit_usd: float | None = Field(
        default=None, ge=0, description="Abort the run if this is exceeded mid-loop."
    )

    # -- handover --------------------------------------------------------------
    #: One line saying what this agent is for. Shown to an agent that may hand
    #: over to it, and it is the whole basis of that decision — so describe the
    #: situation it handles, not the job title. "Refunds, billing disputes and
    #: chargebacks" beats "billing agent".
    purpose: str = Field(
        default="",
        max_length=300,
        title="What this agent handles",
        description="Read by an agent deciding whether to hand over to this one.",
    )
    #: Whether this agent may hand the conversation to an agent wired to its
    #: handover port. Off unless something is wired there, so an agent that
    #: works alone is never told about a mechanism it cannot use.
    handover: bool = Field(default=True, title="Allow handover")

    # -- skills ----------------------------------------------------------------
    #: Ids of skills from the workspace library this agent may use.
    #:
    #: The agent is told only their names and descriptions; it reads a body by
    #: calling `load_skill`, so listing ten skills costs ten lines of prompt
    #: rather than ten procedures. Skills deleted from the library are skipped
    #: with a `skill.missing` step rather than failing the run.
    skills: list[str] = Field(default_factory=list, max_length=25, title="Skills")
    #: Total characters of skill text one run may pull in. The ceiling that
    #: keeps "the agent may consult the library" from meaning "every run
    #: carries the library".
    skill_budget_chars: int = Field(
        default=60000, ge=1000, le=400000, title="Skill budget (characters)"
    )

    # -- memory ----------------------------------------------------------------
    #: Whether this agent remembers earlier runs.
    #:
    #: "off" — every run starts blank. Correct for one-shot work: a classifier
    #: that summarises whatever arrives has nothing to gain from last week's
    #: input, and remembering would only bias it.
    #:
    #: "conversation" — the human turn and the final reply of previous runs are
    #: replayed before the new prompt, so the agent can be told "the fix you
    #: suggested didn't work" and know what fix that was.
    memory: Literal["off", "conversation"] = Field(default="off", title="Memory")
    #: Which conversation to load. Supports {{ references }}, and normally
    #: should use one: `{{ input.payload.issue.number }}` keeps one thread per
    #: GitHub issue, `{{ input.payload.chat_id }}` one per chat. An empty key
    #: means a single shared thread for the node, which is right for a standing
    #: assistant and wrong for anything with more than one counterparty —
    #: they would read each other's history.
    memory_key: str = Field(
        default="",
        max_length=300,
        title="Memory key",
        description="One thread per rendered value. Supports {{ references }}.",
    )
    #: How many past turns to replay, newest kept. This is a cost control as
    #: much as a relevance one: history is resent in full on every model call,
    #: so an unbounded memory makes each run more expensive than the last until
    #: it hits the context limit.
    memory_window: int = Field(default=20, ge=2, le=200, title="Memory window")

    # -- output ----------------------------------------------------------------
    response_format: Literal["text", "json"] = "text"

    # -- transport -------------------------------------------------------------
    base_url: str = Field(default="", max_length=300, description="Overrides the credential's.")
    request_timeout_seconds: float = Field(default=120.0, ge=5, le=600)


def _handover_tools(
    ctx: NodeContext, colleagues: list[dict[str, str]], chosen: dict[str, str]
) -> list[Any]:
    """One `transfer_to_…` tool per agent wired to the handover port.

    Deliberately not a sub-agent running inside this one. The colleague is a
    node on the canvas: it has its own model, tools, skills, memory and cost
    row, and its turn appears in the run log as its own step rather than as a
    nested stream inside this node's. What one agent may hand to another is
    then an edge someone drew, and a question anyone can answer by looking.
    """
    from basivo_orch.flows.nodes.agent_runtime import build_tool

    tools: list[Any] = []
    for colleague in colleagues:
        target_id = colleague["id"]
        name = _tool_name(colleague["name"])

        async def transfer(reason: str = "", _id: str = target_id, _label: str = colleague["name"]):
            # Recorded, not just acted on: "who decided to escalate this, and
            # why" is the first question asked of any handover after the fact.
            chosen["id"] = _id
            chosen["reason"] = reason
            await ctx.step("agent.handover", {"to": _label, "to_id": _id, "reason": reason[:400]})
            return f"Handed over to {_label}. Stop and say nothing further."

        tools.append(
            build_tool(
                name=f"transfer_to_{name}",
                description=(
                    f"Hand this conversation to {colleague['name']}"
                    + (f", which handles: {colleague['purpose']}" if colleague["purpose"] else "")
                    + ". Use it when the request is theirs rather than yours. They reply to the "
                    "person directly, so do not answer as well."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why this belongs to them, in one line.",
                        }
                    },
                    "required": [],
                },
                execute=transfer,
            )
        )
    return tools


def _tool_name(label: str) -> str:
    """A node name as something a model can type as a tool call."""
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug[:40] or "agent"


async def _load_skills(
    config: AgentConfig, ctx: NodeContext
) -> tuple[list[LoadedSkill], list[Any]]:
    """Fetch the selected skills and build the tools that read them.

    Returns ([], []) when none are selected, so an agent without skills gets no
    extra tools at all — an empty `load_skill` in the list would be a tool the
    model can only fail with.
    """
    if not config.skills or ctx.load_skills is None:
        return [], []

    skills = await ctx.load_skills(list(config.skills))
    missing = len(config.skills) - len(skills)
    if missing > 0:
        # Named as a count, not ids: the ids are in the graph, and what the
        # person reading the log needs to know is that the agent was offered
        # less than the flow says it was.
        await ctx.step(
            "skill.missing",
            {
                "expected": len(config.skills),
                "found": len(skills),
                "note": "Skills removed from the library are skipped.",
            },
        )
    if not skills:
        return [], []

    budget = SkillBudget(limit=config.skill_budget_chars)
    tools = skill_tools(ctx, skills, budget=budget)
    await ctx.step(
        "skill.offered",
        {
            "skills": [skill.name for skill in skills],
            "budget_chars": budget.limit,
        },
    )
    return skills, tools


def _memory_subject(config: AgentConfig, template: dict[str, Any]) -> str:
    """Which conversation this run belongs to.

    `"default"` when no key is configured, rather than the empty string: a
    blank subject in the table would be indistinguishable from a key that
    rendered empty because its reference was missing, and those two want very
    different handling — the first is one shared thread, the second is a bug
    that would silently merge every counterparty into it.
    """
    if not config.memory_key.strip():
        return "default"
    rendered = render_value(config.memory_key, template)
    if not isinstance(rendered, str):
        rendered = json.dumps(rendered, default=str, sort_keys=True)
    rendered = rendered.strip()
    if not rendered:
        raise NodeError(
            f"The memory key {config.memory_key!r} rendered empty. It decides whose "
            "conversation this is, so an empty value would mix separate threads "
            "together. Check the reference against the trigger's payload."
        )
    return rendered[:300]


async def _load_memory(
    config: AgentConfig, ctx: NodeContext, template: dict[str, Any]
) -> list[dict[str, Any]]:
    if config.memory == "off" or ctx.load_memory is None:
        return []
    subject = _memory_subject(config, template)
    turns = await ctx.load_memory(ctx.node_id, subject)
    # Windowed on the way in as well as out: a window narrowed after a long
    # conversation must take effect on the next run, not gradually.
    turns = turns[-config.memory_window :]
    await ctx.step(
        "memory.loaded",
        {"subject": subject, "turns": len(turns), "window": config.memory_window},
    )
    return turns


async def _save_memory(
    config: AgentConfig,
    ctx: NodeContext,
    template: dict[str, Any],
    history: list[dict[str, Any]],
    prompt: str,
    text: str,
) -> None:
    """Append this exchange to what the agent remembers.

    Saved even when the reply is empty — a truncated or cost-limited run still
    happened, and dropping the human turn would make the next run answer a
    question it appears never to have been asked.
    """
    if config.memory == "off" or ctx.save_memory is None:
        return
    subject = _memory_subject(config, template)
    turns = [*history, {"role": "user", "text": prompt[:8000]}]
    if text.strip():
        turns.append({"role": "assistant", "text": text[:8000]})
    turns = turns[-config.memory_window :]
    await ctx.save_memory(ctx.node_id, subject, turns)
    await ctx.step("memory.saved", {"subject": subject, "turns": len(turns)})


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
    output_paths = (
        "text",
        "json",
        "stop_reason",
        "handover_to",
        "handover_reason",
        "tool_calls",
        "usage.input_tokens",
        "usage.output_tokens",
        "usage.cost_usd",
    )

    #: A second way out, for handing the conversation to another agent node.
    #: Wire it to other agents on the canvas: whatever is connected becomes the
    #: team, so who can talk to whom is a thing you can see rather than a list
    #: buried in one node's configuration.
    ports = (DEFAULT_PORT, HANDOVER_PORT)

    max_attempts = 2
    retry_backoff_seconds = 2.0
    #: Generous: the ceiling is the loop (`max_iterations` / `max_tool_calls`),
    #: and a slow tool should not be killed mid-call by a timeout tuned for a
    #: single HTTP request.
    timeout_seconds = 660.0

    async def run(self, config: AgentConfig, ctx: NodeContext) -> NodeResult:
        model = await build_chat_model(
            ctx,
            provider=config.provider,
            model=config.model,
            credential_id=config.credential_id,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            top_p=config.top_p,
            request_timeout=config.request_timeout_seconds,
            stop=config.stop_sequences or None,
        )

        skills, skill_extras = await _load_skills(config, ctx)

        template = ctx.template_context()
        system = str(render_value(config.system, template)) if config.system else ""
        prompt = render_value(config.prompt, template)
        if not isinstance(prompt, str):
            prompt = json.dumps(prompt, default=str)
        if not prompt.strip():
            raise NodeError("The prompt rendered empty. There is nothing to send.")

        if config.response_format == "json":
            # PromptedOutput/NativeOutput want a concrete schema to validate
            # against; "any JSON object the model chooses to return" is not
            # one. Plain text output plus a best-effort parse (`_parse_json`)
            # is the more honest fit — and it degrades to a clear NodeError
            # instead of an opaque `UnexpectedModelBehavior` from a validator
            # that was never going to accept an open-ended shape.
            system = (f"{system}\n\nRespond with a single JSON object and nothing else.").strip()

        if skills:
            system = (system + skill_catalogue(skills)).strip()

        tool_budget = {"used": 0}

        def guard(definition: ToolDefinition):
            """Wrap one user-defined tool with logging and the call ceiling."""

            async def call(**arguments: Any) -> Any:
                if tool_budget["used"] >= config.max_tool_calls:
                    # Refused as a result, not an exception: the model is told
                    # why and can still write its answer, instead of the run
                    # dying with a half-finished thought.
                    return (
                        f"Refused: this run has already made {config.max_tool_calls} tool "
                        "calls, its limit. Answer with what you have."
                    )
                tool_budget["used"] += 1
                await ctx.step(
                    "tool.called",
                    {"tool": definition.name, "kind": definition.kind, "arguments": arguments},
                )
                started = time.perf_counter()
                ok, output = await _execute_tool(definition, arguments, ctx, template)
                await ctx.step(
                    "tool.result",
                    {
                        "tool": definition.name,
                        "ok": ok,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "result_preview": str(output)[:400],
                    },
                )
                return output

            return build_tool(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
                execute=call,
            )

        # Whoever the author wired to the handover port. An empty list when
        # nothing is connected, so an agent working alone is never told about a
        # mechanism it has no use for.
        colleagues = (
            [
                node
                for node in (ctx.downstream(HANDOVER_PORT) if ctx.downstream else [])
                if node["type"] == AgentNode.type
            ]
            if config.handover
            else []
        )
        handover: dict[str, str] = {}

        totals = RunTotals()
        # Skill tools first: they are the ones the catalogue told the model
        # about by name, and a model scanning a tool list finds them sooner.
        tools = [
            *skill_extras,
            *_handover_tools(ctx, colleagues, handover),
            *[guard(definition) for definition in config.tools],
        ]

        if colleagues:
            system = (
                system
                + "\n\n## Other agents you can hand this to\n\n"
                + "\n".join(
                    f"- {c['name']}" + (f" — {c['purpose']}" if c["purpose"] else "")
                    for c in colleagues
                )
                + "\n\nIf the request belongs to one of them, call its transfer tool and stop. "
                "If it is yours, answer it yourself and do not transfer."
            ).strip()

        if config.sub_agents and config.team_mode == "handover":
            await ctx.step(
                "agent.started",
                {
                    "provider": config.provider,
                    "model": config.model,
                    "mode": "handover",
                    "team": ["main", *[sub.name for sub in config.sub_agents]],
                    "tools": [tool.name for tool in config.tools],
                    "skills": [skill.name for skill in skills],
                },
            )
            members = [
                TeamMember(
                    name="main",
                    model=model,
                    system=system,
                    tools=tools,
                    description="The agent that takes the request first.",
                    model_name=config.model,
                    provider=config.provider,
                )
            ]
            for sub in config.sub_agents:
                members.append(
                    TeamMember(
                        name=sub.name,
                        model=await build_chat_model(
                            ctx,
                            provider=sub.provider if sub.model else config.provider,
                            model=sub.model or config.model,
                            credential_id=sub.credential_id or config.credential_id,
                            temperature=sub.temperature,
                            max_tokens=sub.max_tokens,
                        ),
                        system=sub.system,
                        description=sub.description,
                        model_name=sub.model or config.model,
                        provider=sub.provider if sub.model else config.provider,
                    )
                )
            history = await _load_memory(config, ctx, template)
            await run_team(
                ctx,
                members=members,
                entry="main",
                prompt=prompt,
                max_iterations=config.max_iterations,
                cost_limit_usd=config.cost_limit_usd,
                totals=totals,
                history=history,
            )
            totals.tool_calls = tool_budget["used"]
            text = totals.text
            await _save_memory(config, ctx, template, history, prompt, text)
            json_output = parse_json(text) if config.response_format == "json" else None
            await ctx.step(
                "agent.finished",
                {
                    "stop_reason": totals.stop_reason,
                    "mode": "handover",
                    "answered_by": totals.answered_by,
                    "handovers": totals.delegations,
                    "tool_calls": totals.tool_calls,
                    "requests": totals.requests,
                    "input_tokens": totals.input_tokens,
                    "output_tokens": totals.output_tokens,
                    "cost_usd": round(totals.cost_usd, 6),
                },
            )
            return NodeResult(
                output={
                    "text": text,
                    "json": json_output,
                    "stop_reason": totals.stop_reason,
                    "tool_calls": totals.tool_calls,
                    "handovers": totals.delegations,
                    "answered_by": totals.answered_by,
                    "usage": {
                        "input_tokens": totals.input_tokens,
                        "output_tokens": totals.output_tokens,
                        "cost_usd": round(totals.cost_usd, 6),
                    },
                },
                metrics={
                    "tokens_in": totals.input_tokens,
                    "tokens_out": totals.output_tokens,
                    "cost_usd": round(totals.cost_usd, 6),
                },
            )

        for sub in config.sub_agents:
            # A sub-agent inherits whatever it did not override, so the common
            # case is a name and a description and nothing else.
            resolved = sub.model_copy(
                update={
                    "model": sub.model or config.model,
                    "provider": sub.provider if sub.model else config.provider,
                    "credential_id": sub.credential_id or config.credential_id,
                }
            )
            tools.append(
                delegation_tool(
                    ctx,
                    sub_agent=resolved,
                    parent_totals=totals,
                    cost_limit_usd=config.cost_limit_usd,
                )
            )

        await ctx.step(
            "agent.started",
            {
                "provider": config.provider,
                "model": config.model,
                "tools": [tool.name for tool in config.tools],
                "skills": [skill.name for skill in skills],
                "sub_agents": [sub.name for sub in config.sub_agents],
                "max_iterations": config.max_iterations,
            },
        )

        history = await _load_memory(config, ctx, template)

        await run_agent(
            ctx,
            model=model,
            prompt=prompt,
            system=system,
            tools=tools,
            max_iterations=config.max_iterations,
            max_tool_calls=config.max_tool_calls,
            cost_limit_usd=config.cost_limit_usd,
            provider=config.provider,
            model_name=config.model,
            totals=totals,
            history=history,
        )
        totals.tool_calls = tool_budget["used"]

        text = totals.text
        await _save_memory(config, ctx, template, history, prompt, text)
        json_output = parse_json(text) if config.response_format == "json" else None

        await ctx.step(
            "agent.finished",
            {
                "stop_reason": totals.stop_reason,
                "tool_calls": totals.tool_calls,
                "requests": totals.requests,
                "input_tokens": totals.input_tokens,
                "output_tokens": totals.output_tokens,
                "cost_usd": round(totals.cost_usd, 6),
                "delegations": totals.delegations,
            },
        )

        handed_to = handover.get("id")
        return NodeResult(
            output={
                # The receiving agent reads `{{ input.text }}` like any other
                # downstream node, so a handover carries the conversation
                # rather than starting a new one. When nothing was said before
                # transferring, the reason is what travels.
                "text": text or handover.get("reason", ""),
                "json": json_output,
                "stop_reason": "handover" if handed_to else totals.stop_reason,
                "tool_calls": totals.tool_calls,
                "delegations": totals.delegations,
                "handover_to": handed_to or "",
                "handover_reason": handover.get("reason", ""),
                "usage": {
                    "input_tokens": totals.input_tokens,
                    "output_tokens": totals.output_tokens,
                    "cost_usd": round(totals.cost_usd, 6),
                },
            },
            # Exactly one of the two ports fires. An agent that transferred has
            # not answered, so continuing down the default edge as well would
            # run the rest of the flow on a non-answer.
            ports=[HANDOVER_PORT] if handed_to else [DEFAULT_PORT],
            route_to=[handed_to] if handed_to else None,
            metrics={
                "tokens_in": totals.input_tokens,
                "tokens_out": totals.output_tokens,
                "cost_usd": round(totals.cost_usd, 6),
            },
        )


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------


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

    if definition.kind == "code":
        try:
            result, _printed = await run_python(
                definition.code,
                # The model's arguments where the function expects them, and
                # the flow context beside them — "get the order" usually needs
                # both what the model asked for and what the flow knows.
                {**template, "args": arguments},
                timeout_seconds=definition.timeout_seconds,
            )
            return True, result
        except PythonExecutionError as exc:
            return False, str(exc)

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
