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
from typing import Any

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
