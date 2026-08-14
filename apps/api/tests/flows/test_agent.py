"""The Agent node.

The risk surface here is entirely in the code we wrote around pydantic-ai —
the iteration loop, what gets logged as a step, how usage and cost are read
back out, how a dynamic tool is wired up and how its failures are reported.
pydantic-ai's own request/response handling is out of scope; `FunctionModel`
stands in for a real provider so these tests exercise our loop without a
network call or an API key.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from basivo_orch.flows.nodes.agent import (
    AgentConfig,
    AgentNode,
    ToolDefinition,
    _construct_provider,
    _parse_json,
)
from basivo_orch.flows.nodes.base import NodeContext, NodeError


class _Recorder:
    """Captures every `ctx.step()` / `ctx.progress()` call for assertions."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.progress_lines: list[str] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        self.progress_lines.append(message)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.steps]

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(recorder: _Recorder, *, http: httpx.AsyncClient) -> NodeContext:
    async def resolve_credential(_credential_id: str):
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="agent_1",
        node_name="Agent",
        attempt=1,
        input={"question": "what is 2+3?"},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


async def test_agent_runs_a_tool_and_logs_every_turn(monkeypatch, http_client):
    """The exact scenario the user asked to verify: attach a tool, run it, and
    be able to see what model call happened, what tool fired, and what it cost
    — as distinct, ordered steps."""
    calls = {"n": 0}

    def fake_model(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="add", args={"a": 2, "b": 3}, tool_call_id="tc1")]
            )
        return ModelResponse(parts=[TextPart(content="The answer is 5.")])

    async def fake_build_model(config, ctx):
        return FunctionModel(fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent._build_model", fake_build_model)

    config = AgentConfig(
        prompt="{{ input.question }}",
        tools=[
            ToolDefinition(
                name="add",
                description="Add two numbers.",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
                kind="constant",
                # A constant tool ignores the model's arguments and returns this
                # verbatim — enough to prove the call reaches a tool and the
                # result flows back to the model, without needing an HTTP tool.
                value=5,
            )
        ],
    )

    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)

    result = await AgentNode().run(config, ctx)

    # The full shape of what happened, in order — this is the "end to end
    # logging" the product exists to provide.
    assert recorder.kinds() == [
        "agent.started",
        "llm.response",
        "tool.called",
        "tool.result",
        "llm.response",
        "agent.finished",
    ]

    called = recorder.data_for("tool.called")[0]
    assert called["tool"] == "add"
    assert called["arguments"] == {"a": 2, "b": 3}

    result_step = recorder.data_for("tool.result")[0]
    assert result_step["ok"] is True
    assert "duration_ms" in result_step

    responses = recorder.data_for("llm.response")
    assert all("input_tokens" in r and "output_tokens" in r for r in responses)
    assert all("duration_ms" in r for r in responses)

    finished = recorder.data_for("agent.finished")[0]
    assert finished["tool_calls"] == 1
    assert finished["input_tokens"] > 0

    assert result.output["text"] == "The answer is 5."
    assert result.output["tool_calls"] == 1
    assert result.metrics["tokens_in"] > 0
    assert result.metrics["tokens_out"] > 0


async def test_agent_response_format_json(monkeypatch, http_client):
    def fake_model(messages, info):
        return ModelResponse(parts=[TextPart(content='```json\n{"answer": 42}\n```')])

    async def fake_build_model(config, ctx):
        return FunctionModel(fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent._build_model", fake_build_model)

    config = AgentConfig(prompt="go", response_format="json")
    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)

    result = await AgentNode().run(config, ctx)

    assert result.output["json"] == {"answer": 42}


async def test_agent_reports_a_tool_error_without_crashing_the_run(monkeypatch, http_client):
    """A tool that fails is data the model sees, not a crashed node — the
    model gets one more turn to react to the failure."""
    calls = {"n": 0}

    def fake_model(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="broken", args={}, tool_call_id="tc1")]
            )
        return ModelResponse(parts=[TextPart(content="The tool failed, so I cannot answer.")])

    async def fake_build_model(config, ctx):
        return FunctionModel(fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent._build_model", fake_build_model)

    config = AgentConfig(
        prompt="go",
        tools=[
            ToolDefinition(
                name="broken",
                kind="http",
                url="",  # no URL configured -> the tool reports failure, not a crash
            )
        ],
    )
    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)

    result = await AgentNode().run(config, ctx)

    tool_result = recorder.data_for("tool.result")[0]
    assert tool_result["ok"] is False
    assert result.output["text"] == "The tool failed, so I cannot answer."


def test_parse_json_handles_fenced_and_bare_json():
    assert _parse_json('{"a": 1}') == {"a": 1}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure, here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_parse_json_raises_a_readable_error_on_garbage():
    with pytest.raises(NodeError):
        _parse_json("not json at all")


def test_construct_provider_only_passes_kwargs_the_constructor_accepts():
    """Bedrock authenticates by AWS signature, not a bearer key — a stored
    credential's `api_key` must not be forced onto a constructor that has no
    such parameter."""

    class FakeBedrockLikeProvider:
        def __init__(self, region_name: str = "us-east-1") -> None:
            self.region_name = region_name

    provider = _construct_provider(
        FakeBedrockLikeProvider,
        api_key="sk-should-be-ignored",
        base_url="",
        options={"region_name": "eu-west-1"},
    )
    assert provider.region_name == "eu-west-1"
    assert not hasattr(provider, "api_key")


def test_construct_provider_passes_api_key_when_accepted():
    class FakeKeyedProvider:
        def __init__(self, api_key: str = "") -> None:
            self.api_key = api_key

    provider = _construct_provider(
        FakeKeyedProvider, api_key="sk-live-123", base_url="", options={}
    )
    assert provider.api_key == "sk-live-123"
