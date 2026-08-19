"""The Agent node.

The risk surface here is entirely in the code we wrote around pydantic-ai —
the iteration loop, what gets logged as a step, how usage and cost are read
back out, how a dynamic tool is wired up and how its failures are reported.
The provider's own request/response handling is out of scope; a scripted model
stands in for a real provider so these tests exercise our loop without a
network call or an API key.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.agent import AgentConfig, AgentNode, ToolDefinition
from basivo_orch.flows.nodes.agent_runtime import parse_json as _parse_json
from basivo_orch.flows.nodes.base import NodeContext, NodeError
from tests.flows.fakes import FakeChatModel, says, tool_call, turn_number


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

    def fake_model(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return tool_call("add", {"a": 2, "b": 3}, call_id="tc1")
        return says("The answer is 5.")

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

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
    def fake_model(messages):
        return says('```json\n{"answer": 42}\n```')

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    config = AgentConfig(prompt="go", response_format="json")
    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)

    result = await AgentNode().run(config, ctx)

    assert result.output["json"] == {"answer": 42}


async def test_agent_reports_a_tool_error_without_crashing_the_run(monkeypatch, http_client):
    """A tool that fails is data the model sees, not a crashed node — the
    model gets one more turn to react to the failure."""
    calls = {"n": 0}

    def fake_model(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return tool_call("broken", {}, call_id="tc1")
        return says("The tool failed, so I cannot answer.")

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    config = AgentConfig(
        prompt="go",
        tools=[
            ToolDefinition(
                name="broken",
                kind="http",
                # Points at loopback, which the SSRF guard refuses at call
                # time — a runtime failure the model gets to react to.
                url="https://127.0.0.1/private",
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


async def test_a_code_tool_runs_the_users_own_function(monkeypatch, http_client):
    """The user's ask, verbatim: a tool that is their own code — get the time,
    get an id, compute something — not an HTTP call. The model's arguments
    arrive at data["args"], the flow context beside them."""
    calls = {"n": 0}

    def fake_model(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            return tool_call("add_up", {"a": 19, "b": 23}, call_id="t1")
        return says("Sum delivered.")

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=fake_model)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build_model)

    config = AgentConfig(
        prompt="add",
        tools=[
            ToolDefinition(
                name="add_up",
                description="Add two numbers.",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
                kind="code",
                code=(
                    "def main(data):\n"
                    '    args = data["args"]\n'
                    '    return {"sum": args["a"] + args["b"], "env": data["vars"]}\n'
                ),
            )
        ],
    )
    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)

    result = await AgentNode().run(config, ctx)

    tool_result = next(data for kind, data in recorder.steps if kind == "tool.result")
    assert tool_result["ok"] is True
    assert (
        "'sum': 42" in tool_result["result_preview"] or '"sum": 42' in tool_result["result_preview"]
    )
    assert result.output["text"] == "Sum delivered."


def test_a_code_tool_without_code_is_refused_at_validation():
    with pytest.raises(Exception, match="code tool with no code"):
        ToolDefinition(name="empty", kind="code")


def test_an_http_tool_without_a_url_is_refused_at_validation():
    with pytest.raises(Exception, match="HTTP tool with no URL"):
        ToolDefinition(name="empty", kind="http")


async def test_an_agent_delegates_to_a_sub_agent_and_uses_its_answer(monkeypatch, http_client):
    """Agent-to-agent, configured rather than wired.

    The parent gets an `ask_<name>` tool per sub-agent; calling it runs that
    agent and its reply comes back as the tool result. Both halves must show
    up on the run log with their own cost, or a delegating agent is a black
    box exactly where the interesting part happens.
    """

    def parent(messages):
        if turn_number(messages) == 0:
            return tool_call(
                "ask_researcher", {"task": "What is the capital of France?"}, call_id="d1"
            )
        return says("The researcher says it is Paris.")

    def researcher(messages):
        return says("Paris.")

    async def fake_build(ctx, **kwargs):
        # The sub-agent is built with its own name in the model field.
        return FakeChatModel(respond=researcher if kwargs.get("model") == "small" else parent)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build)
    monkeypatch.setattr("basivo_orch.flows.nodes.agent_runtime.build_chat_model", fake_build)

    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)
    result = await AgentNode().run(
        AgentConfig(
            prompt="Ask the researcher for the capital of France.",
            model="big",
            sub_agents=[
                {
                    "name": "researcher",
                    "description": "Looks facts up.",
                    "model": "small",
                    "system": "Answer in one word.",
                }
            ],
        ),
        ctx,
    )

    assert result.output["text"] == "The researcher says it is Paris."
    assert result.output["delegations"] == ["researcher"]

    handoff = recorder.data_for("agent.delegated")[0]
    assert handoff["to"] == "researcher"
    assert "Paris" in handoff["reply_preview"]

    # The sub-agent's tokens are counted against the parent's run, so a
    # delegating agent cannot hide its spending.
    assert result.output["usage"]["input_tokens"] > 0
    assert recorder.data_for("agent.finished")[0]["delegations"] == ["researcher"]


async def test_a_sub_agent_inherits_the_parents_model_when_it_names_none(monkeypatch, http_client):
    """The common case is a name and a description and nothing else."""
    seen_models: list[str] = []

    def parent(messages):
        if turn_number(messages) == 0:
            return tool_call("ask_helper", {"task": "do it"}, call_id="d1")
        return says("done")

    def helper(messages):
        return says("helped")

    async def fake_build(ctx, **kwargs):
        seen_models.append(kwargs.get("model"))
        # First build is the parent's; the second is the sub-agent's.
        return FakeChatModel(respond=parent if len(seen_models) == 1 else helper)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", fake_build)
    monkeypatch.setattr("basivo_orch.flows.nodes.agent_runtime.build_chat_model", fake_build)

    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)
    await AgentNode().run(
        AgentConfig(
            prompt="delegate",
            provider="openai",
            model="parent-model",
            sub_agents=[{"name": "helper"}],
        ),
        ctx,
    )

    assert seen_models == ["parent-model", "parent-model"], seen_models
