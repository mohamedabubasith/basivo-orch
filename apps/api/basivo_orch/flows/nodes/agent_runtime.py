"""The agent loop, on LangGraph.

This replaced a pydantic-ai loop, and the migration had one hard requirement:
**the run log must not get worse.** Per-pipeline observability is the product's
differentiator, so every step the old loop emitted is emitted here — one
`llm.response` per model call with its own tokens, duration and cost, a
`tool.call`/`tool.result` pair per tool, and a final `agent.finished` with the
totals. LangGraph streams updates per graph node, which is what makes that
possible without guessing.

Three things LangGraph does not do for us, implemented here because a runaway
agent is a bill:

* **tool-call ceiling** — counted in the wrapper; past the limit the tool
  refuses and tells the model why, rather than the run dying mid-thought.
* **cost ceiling** — accumulated after every model response and enforced
  between turns, because that is the only safe place to stop.
* **cost at all** — LangChain reports tokens and never money; `price_of`
  converts.

Sub-agents are delegation tools: a supervisor calls `ask_<name>`, that runs a
nested agent, and its answer comes back as the tool result. Chosen over
free-form handoffs because it keeps one agent in charge, terminates, and each
delegation is a step on the log with its own tokens.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

# At module scope, not inside the factory: the @tool decorator resolves a
# function's annotations against its module globals, and `from __future__
# import annotations` makes them strings — so a locally-imported name is
# invisible when they are evaluated, and the handoff tool fails to build.
from langchain_core.tools import InjectedToolCallId
from langgraph.types import Command

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.models import build_chat_model, price_of


#: LangGraph counts every graph step, not every model turn: one turn is a
#: model node plus a tool node. Doubling the user's iteration budget (plus a
#: little) turns "how many times may it think" into a recursion limit.
def recursion_limit_for(max_iterations: int) -> int:
    return max_iterations * 2 + 2


class CostLimitReached(Exception):
    """Raised between turns when the run has spent its allowance."""


@dataclass
class RunTotals:
    """What the whole agent run consumed."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    requests: int = 0
    tool_calls: int = 0
    stop_reason: str = "end_turn"
    text: str = ""
    #: Which agent produced the final answer. Only meaningful for a handover
    #: team, where that is not necessarily the one that started.
    answered_by: str = ""
    delegations: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def message_text(message: Any) -> str:
    """The text of an AI message, whatever shape the provider used.

    Content is a plain string on most providers and a list of blocks on the
    ones that stream reasoning separately; a bare `str(content)` there would
    put Python list syntax into the user's output.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content or "")


def parse_json(text: str) -> Any:
    """JSON from a model that was asked for JSON. Raises if there is none.

    Models fence their JSON, or introduce it politely. Neither is a failure
    worth failing a run over, so the fenced body and the outermost braces are
    both tried. But a node configured for JSON output that produces prose has
    failed at the thing it was asked to do, and downstream nodes template into
    its fields — so that raises here rather than passing null along and
    breaking somewhere less obvious.
    """
    if not text:
        raise NodeError(
            "The agent was asked for JSON and returned nothing. Check the prompt, or "
            "switch this node's response format back to text."
        )
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate[3:]
        if candidate.lstrip().lower().startswith("json"):
            candidate = candidate.lstrip()[4:]
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                continue
    raise NodeError(
        "The agent was asked for JSON but its reply could not be parsed as JSON. "
        f"It said: {text[:300]}"
    )


def build_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    execute: Callable[..., Awaitable[str]],
):
    """A LangChain tool whose arguments are a schema the *user* typed.

    `args_schema` takes the JSON schema verbatim, so what the model is told
    about the arguments and what the executor receives are the same object —
    the property that made tool definitions editable in the UI at all.
    """
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        coroutine=execute,
        name=name,
        description=description or f"Call {name}.",
        args_schema=input_schema or {"type": "object", "properties": {}},
    )


async def run_agent(
    ctx: NodeContext,
    *,
    model: Any,
    prompt: str,
    system: str,
    tools: list[Any],
    max_iterations: int,
    max_tool_calls: int,
    cost_limit_usd: float | None,
    provider: str,
    model_name: str,
    totals: RunTotals | None = None,
    label: str = "agent",
) -> RunTotals:
    """Run one agent to completion, logging every step. Returns the totals.

    `totals` is passed in when a sub-agent shares its parent's budget, so a
    supervisor cannot escape its cost ceiling by delegating.
    """
    from langchain.agents import create_agent
    from langgraph.errors import GraphRecursionError

    totals = totals or RunTotals()
    agent = create_agent(model, tools, system_prompt=system or None)

    turn_started = time.perf_counter()
    final_text = ""

    try:
        async for _, chunk in agent.astream(
            {"messages": [("user", prompt)]},
            stream_mode=["updates"],
            config={"recursion_limit": recursion_limit_for(max_iterations)},
        ):
            for node_name, payload in (chunk or {}).items():
                for message in (payload or {}).get("messages", []) or []:
                    kind = type(message).__name__
                    if kind != "AIMessage":
                        continue

                    usage = getattr(message, "usage_metadata", None) or {}
                    tokens_in = int(usage.get("input_tokens") or 0)
                    tokens_out = int(usage.get("output_tokens") or 0)
                    cost = price_of(
                        model=model_name,
                        provider=provider,
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                    )
                    totals.input_tokens += tokens_in
                    totals.output_tokens += tokens_out
                    totals.cost_usd += cost or 0.0
                    totals.requests += 1

                    text = message_text(message)
                    raw_calls = getattr(message, "tool_calls", None) or []
                    tool_calls = [c.get("name") for c in raw_calls]
                    await ctx.step(
                        "llm.response",
                        {
                            "agent": label,
                            "model": model_name,
                            "provider": provider,
                            "node": node_name,
                            "duration_ms": int((time.perf_counter() - turn_started) * 1000),
                            "input_tokens": tokens_in,
                            "output_tokens": tokens_out,
                            "cost_usd": round(cost, 6) if cost is not None else None,
                            "tool_calls": tool_calls,
                            "text_preview": text[:400],
                        },
                    )
                    turn_started = time.perf_counter()
                    if text:
                        final_text = text

                    if cost_limit_usd is not None and totals.cost_usd > cost_limit_usd:
                        # Between turns is the only safe place to stop: mid-turn
                        # would abandon a tool call the model already committed to.
                        raise CostLimitReached(
                            f"spent ${totals.cost_usd:.4f} of a ${cost_limit_usd:.4f} limit"
                        )
    except CostLimitReached as exc:
        totals.stop_reason = "cost_limit"
        await ctx.step("agent.truncated", {"agent": label, "reason": str(exc)})
    except GraphRecursionError:
        totals.stop_reason = "iteration_limit"
        await ctx.step(
            "agent.truncated",
            {"agent": label, "reason": f"reached the {max_iterations}-iteration limit"},
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are retryable
        raise NodeError(f"The model provider returned an error: {exc}", retryable=True) from exc

    totals.text = final_text
    return totals


def delegation_tool(
    ctx: NodeContext,
    *,
    sub_agent: Any,
    parent_totals: RunTotals,
    cost_limit_usd: float | None,
):
    """A tool that hands a task to another agent and returns its answer.

    This is what "agent to agent" is made of. The sub-agent shares the
    parent's totals, so delegation cannot be used to escape a cost ceiling,
    and every hand-off is a step on the run log with the tokens it cost.
    """

    async def delegate(task: str) -> str:
        await ctx.step("agent.delegating", {"to": sub_agent.name, "task": task[:400]})
        await ctx.progress(f"Asking {sub_agent.name}")

        model = await build_chat_model(
            ctx,
            provider=sub_agent.provider,
            model=sub_agent.model,
            credential_id=sub_agent.credential_id,
            temperature=sub_agent.temperature,
            max_tokens=sub_agent.max_tokens,
        )
        before = parent_totals.cost_usd
        result = await run_agent(
            ctx,
            model=model,
            prompt=task,
            system=sub_agent.system,
            tools=[],
            max_iterations=sub_agent.max_iterations,
            max_tool_calls=0,
            cost_limit_usd=cost_limit_usd,
            provider=sub_agent.provider,
            model_name=sub_agent.model,
            totals=parent_totals,
            label=sub_agent.name,
        )
        parent_totals.delegations.append(sub_agent.name)
        await ctx.step(
            "agent.delegated",
            {
                "to": sub_agent.name,
                "cost_usd": round(parent_totals.cost_usd - before, 6),
                "reply_preview": result.text[:400],
            },
        )
        return result.text or "(the sub-agent returned nothing)"

    return build_tool(
        name=f"ask_{sub_agent.name}",
        description=(
            sub_agent.description or f"Delegate a task to {sub_agent.name} and receive its answer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What you want this agent to do. Give it full context — "
                    "it cannot see your conversation.",
                }
            },
            "required": ["task"],
        },
        execute=delegate,
    )


# ---------------------------------------------------------------------------
# Handover: control moves, rather than an answer coming back
# ---------------------------------------------------------------------------
#
# Delegation and handover are different shapes and neither replaces the other.
#
# * **Delegate** (`ask_<name>`): the parent asks, gets an answer, and keeps
#   control. Right for "look this up for me" — the parent still writes the
#   reply, so it can combine several answers.
# * **Handover** (`transfer_to_<name>`): control *moves*. The receiving agent
#   answers the user directly and may transfer on to a third. Right for triage
#   — a front desk that routes to billing, which routes to refunds — where
#   pretending the front desk wrote the final answer would be a lie.
#
# LangGraph makes the second possible with `Command(goto=..., graph=PARENT)`
# returned from a tool: it escapes the agent's own subgraph and routes in the
# team graph the agents are nodes of. The conversation is shared state, so the
# receiving agent sees everything said so far rather than a summary.


def handoff_tool(target: str, description: str):
    """A tool that transfers the conversation to another agent."""
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    @tool(f"transfer_to_{target}", description=description)
    def transfer(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
        return Command(
            goto=target,
            graph=Command.PARENT,
            # The tool call must be answered even though control is leaving:
            # a provider that receives an assistant turn with an unanswered
            # tool call rejects the whole conversation on the next request.
            update={
                "messages": [
                    ToolMessage(
                        content=f"Transferred to {target}.",
                        tool_call_id=tool_call_id,
                        name=f"transfer_to_{target}",
                    )
                ]
            },
        )

    return transfer


@dataclass
class TeamMember:
    """One agent in a handover team."""

    name: str
    model: Any
    system: str
    tools: list[Any] = field(default_factory=list)
    description: str = ""
    model_name: str = ""
    provider: str = ""


async def run_team(
    ctx: NodeContext,
    *,
    members: list[TeamMember],
    entry: str,
    prompt: str,
    max_iterations: int,
    cost_limit_usd: float | None,
    totals: RunTotals | None = None,
) -> RunTotals:
    """Run a team where control hands over between agents.

    Every member can transfer to every other member, so a conversation can go
    front-desk → billing → refunds without the front desk having to know the
    whole map. Each agent's turns are logged under its own name, which is the
    only way to answer "who actually said that" afterwards.
    """
    from langchain.agents import create_agent
    from langgraph.errors import GraphRecursionError
    from langgraph.graph import END, START, MessagesState, StateGraph

    totals = totals or RunTotals()
    by_name = {member.name: member for member in members}

    graph = StateGraph(MessagesState)
    for member in members:
        transfers = [
            handoff_tool(
                other.name,
                other.description or f"Hand the conversation to {other.name}.",
            )
            for other in members
            if other.name != member.name
        ]
        agent = create_agent(
            member.model,
            [*member.tools, *transfers],
            system_prompt=(member.system or None),
        )
        graph.add_node(member.name, agent)
        graph.add_edge(member.name, END)
    graph.add_edge(START, entry)
    app = graph.compile()

    speaking = entry
    turn_started = time.perf_counter()

    try:
        # `subgraphs=True` is essential rather than a detail: each agent is a
        # compiled graph of its own, and without it the transferring agent's
        # turn — and its tokens — never surface at all. The namespace names
        # the agent that spoke; the parent-level updates name the routing.
        async for namespace, _, chunk in app.astream(
            {"messages": [("user", prompt)]},
            stream_mode=["updates"],
            subgraphs=True,
            config={"recursion_limit": recursion_limit_for(max_iterations) * max(1, len(members))},
        ):
            inside = namespace[0].split(":")[0] if namespace else ""

            # The handover is recorded when the new agent starts speaking,
            # not when the parent-level routing update arrives — that lands
            # only after the receiving agent has already answered, which put
            # the hand-off *after* its own consequence in the timeline.
            if inside and inside in by_name and inside != speaking:
                totals.delegations.append(inside)
                await ctx.step("agent.handover", {"from": speaking, "to": inside})
                await ctx.progress(f"{speaking} handed over to {inside}")
                speaking = inside

            for node_name, payload in (chunk or {}).items():
                # Model turns are counted once, from inside the agent that made
                # them — the parent level repeats the same message, and counting
                # both would double every token.
                if not inside:
                    continue
                member = by_name.get(inside)
                for message in (payload or {}).get("messages", []) or []:
                    if type(message).__name__ != "AIMessage":
                        continue
                    usage = getattr(message, "usage_metadata", None) or {}
                    tokens_in = int(usage.get("input_tokens") or 0)
                    tokens_out = int(usage.get("output_tokens") or 0)
                    cost = price_of(
                        model=member.model_name if member else "",
                        provider=member.provider if member else "",
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                    )
                    totals.input_tokens += tokens_in
                    totals.output_tokens += tokens_out
                    totals.cost_usd += cost or 0.0
                    totals.requests += 1

                    text = message_text(message)
                    await ctx.step(
                        "llm.response",
                        {
                            "agent": inside,
                            "model": member.model_name if member else "",
                            "duration_ms": int((time.perf_counter() - turn_started) * 1000),
                            "input_tokens": tokens_in,
                            "output_tokens": tokens_out,
                            "cost_usd": round(cost, 6) if cost is not None else None,
                            "tool_calls": [
                                c.get("name") for c in (getattr(message, "tool_calls", None) or [])
                            ],
                            "text_preview": text[:400],
                        },
                    )
                    turn_started = time.perf_counter()
                    if text:
                        totals.text = text

                    if cost_limit_usd is not None and totals.cost_usd > cost_limit_usd:
                        raise CostLimitReached(
                            f"spent ${totals.cost_usd:.4f} of a ${cost_limit_usd:.4f} limit"
                        )
    except CostLimitReached as exc:
        totals.stop_reason = "cost_limit"
        await ctx.step("agent.truncated", {"agent": speaking, "reason": str(exc)})
    except GraphRecursionError:
        totals.stop_reason = "iteration_limit"
        await ctx.step(
            "agent.truncated",
            {"agent": speaking, "reason": f"the team passed the {max_iterations}-iteration limit"},
        )
    except Exception as exc:  # noqa: BLE001 — provider errors are retryable
        raise NodeError(f"The model provider returned an error: {exc}", retryable=True) from exc

    totals.answered_by = speaking
    return totals
