"""A chat model that answers from a script, for tests that must not use a key.

The suite runs with no network and no credentials, which is what keeps it at
fifteen seconds and lets it run in CI. That property was previously provided
by pydantic-ai's `FunctionModel`; this is its LangChain counterpart, and it is
deliberately the same shape — a function that sees the conversation so far and
returns the next reply — so a test reads as a transcript.

It reports token usage like a real provider, so the cost and token accounting
in `agent_runtime` is exercised rather than skipped.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def says(text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> AIMessage:
    """A plain answer."""
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def tool_call(name: str, args: dict[str, Any], *, call_id: str = "call-1") -> AIMessage:
    """A turn that asks for one tool."""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        usage_metadata={"input_tokens": 12, "output_tokens": 6, "total_tokens": 18},
    )


def turn_number(messages: list[BaseMessage]) -> int:
    """How many replies this model has already given — the script's cursor."""
    return sum(1 for message in messages if isinstance(message, AIMessage))


class FakeChatModel(BaseChatModel):
    """Returns whatever `respond` says, given the conversation so far."""

    respond: Callable[[list[BaseMessage]], AIMessage]

    @property
    def _llm_type(self) -> str:
        return "fake-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeChatModel:
        # create_agent binds tools before running. The script decides what to
        # call, so the binding itself is a no-op — but it must exist, or the
        # agent cannot be built at all.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        # A script may be async — the concurrency tests sleep inside it to
        # prove two agent nodes really do overlap.
        reply = self.respond(messages)
        if inspect.isawaitable(reply):
            reply = await reply
        return ChatResult(generations=[ChatGeneration(message=reply)])
