"""The Write with AI node.

Everything worth testing here is what we wrote around one `ainvoke`: template
resolution, the system prompt, the JSON contract and what happens when the
model does not honour it, and the usage numbers the run detail page reads. A
scripted model stands in for a provider, so the suite still needs no API key.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.llm import LlmGenerateConfig, LlmGenerateNode
from tests.flows.fakes import FakeChatModel, says


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.progress_lines: list[str] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        self.progress_lines.append(message)

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(
    recorder: _Recorder,
    *,
    http: httpx.AsyncClient,
    node_input: object = None,
    outputs: dict | None = None,
) -> NodeContext:
    async def resolve_credential(_credential_id: str):
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="write_1",
        node_name="Write with AI",
        attempt=1,
        input=node_input if node_input is not None else {"topic": "otters"},
        outputs=outputs or {},
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


def scripted(monkeypatch, respond) -> list[list]:
    """Install a scripted model and hand back the conversations it was sent."""
    seen: list[list] = []

    def capture(messages):
        seen.append(list(messages))
        return respond(messages)

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=capture)

    monkeypatch.setattr("basivo_orch.flows.nodes.llm.build_chat_model", fake_build_model)
    return seen


async def test_plain_text_generation_returns_the_reply(monkeypatch, http_client):
    scripted(monkeypatch, lambda _messages: says("Otters hold hands while they sleep."))

    recorder = _Recorder()
    result = await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Write one fact."),
        make_context(recorder, http=http_client),
    )

    assert result.output["text"] == "Otters hold hands while they sleep."
    # Nothing was asked for as JSON, so nothing is invented at that path.
    assert result.output["json"] is None
    assert recorder.progress_lines


async def test_json_output_parses_a_fenced_reply(monkeypatch, http_client):
    """Models fence their JSON however firmly they are told not to. That is a
    formatting habit, not a failed run."""
    fenced = '```json\n{"title": "Otters", "body": "They hold hands."}\n```'
    scripted(monkeypatch, lambda _messages: says(fenced))

    recorder = _Recorder()
    result = await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Write a post.", output="json"),
        make_context(recorder, http=http_client),
    )

    assert result.output["json"] == {"title": "Otters", "body": "They hold hands."}
    # The raw reply is kept as well: a later node may want the text, and the
    # fences are what someone debugging a parse needs to see.
    assert result.output["text"] == fenced


async def test_a_reply_that_is_not_json_fails_with_something_to_do(monkeypatch, http_client):
    scripted(monkeypatch, lambda _messages: says("Sure! Here is a lovely post about otters."))

    recorder = _Recorder()
    with pytest.raises(NodeError) as raised:
        await LlmGenerateNode().run(
            LlmGenerateConfig(prompt="Write a post.", output="json"),
            make_context(recorder, http=http_client),
        )

    message = str(raised.value)
    assert "lovely post about otters" in message
    assert "instructions" in message and "plain text" in message
    # And the reply is on the run log, so the failure can be read back after
    # the fact rather than reproduced.
    assert recorder.data_for("llm.response")[0]["text"].startswith("Sure!")


async def test_the_prompt_resolves_references_to_an_earlier_node(monkeypatch, http_client):
    seen = scripted(monkeypatch, lambda _messages: says("done"))

    recorder = _Recorder()
    ctx = make_context(
        recorder,
        http=http_client,
        outputs={"research": {"summary": "Otters use tools."}},
    )
    await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Expand this: {{ nodes.research.output.summary }}"),
        ctx,
    )

    assert seen[0][-1].content == "Expand this: Otters use tools."


async def test_the_system_prompt_reaches_the_model(monkeypatch, http_client):
    seen = scripted(monkeypatch, lambda _messages: says("ok"))

    recorder = _Recorder()
    await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Write a post.", system="Write for {{ input.topic }} keepers."),
        make_context(recorder, http=http_client),
    )

    assert seen[0][0].content == "Write for otters keepers."


async def test_json_output_tells_the_model_what_shape_to_reply_in(monkeypatch, http_client):
    """Without this instruction the JSON setting would be a promise with
    nothing behind it: the model has no other way to know."""
    seen = scripted(monkeypatch, lambda _messages: says('{"ok": true}'))

    recorder = _Recorder()
    await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Write a post.", output="json"),
        make_context(recorder, http=http_client),
    )

    assert "single JSON object" in seen[0][0].content


async def test_tokens_and_cost_are_recorded(monkeypatch, http_client):
    scripted(
        monkeypatch,
        lambda _messages: says("A paragraph.", input_tokens=120, output_tokens=40),
    )

    recorder = _Recorder()
    result = await LlmGenerateNode().run(
        LlmGenerateConfig(prompt="Write.", provider="openai", model="gpt-4o-mini"),
        make_context(recorder, http=http_client),
    )

    assert result.output["usage"]["input_tokens"] == 120
    assert result.output["usage"]["output_tokens"] == 40
    # A priced model gives a real number; the point is that the field exists
    # and matches the metrics row the run page reads.
    assert result.output["usage"]["cost_usd"] == result.metrics["cost_usd"]
    assert result.metrics["tokens_in"] == 120 and result.metrics["tokens_out"] == 40
    assert recorder.data_for("llm.response")[0]["model"] == "gpt-4o-mini"


async def test_an_empty_reply_is_a_failure_not_an_empty_post(monkeypatch, http_client):
    """Passing "" down the graph publishes a blank post. Failing here names the
    two settings that cause it."""
    scripted(monkeypatch, lambda _messages: says("   "))

    recorder = _Recorder()
    with pytest.raises(NodeError) as raised:
        await LlmGenerateNode().run(
            LlmGenerateConfig(prompt="Write."),
            make_context(recorder, http=http_client),
        )

    assert "replied with nothing" in str(raised.value)
    assert "maximum length" in str(raised.value)


async def test_a_prompt_that_renders_empty_never_reaches_the_model(monkeypatch, http_client):
    """An upstream node that returned nothing would otherwise be billed as a
    model call that asks the model nothing."""
    called = {"n": 0}

    def respond(_messages):
        called["n"] += 1
        return says("never reached")

    scripted(monkeypatch, respond)

    recorder = _Recorder()
    with pytest.raises(NodeError) as raised:
        await LlmGenerateNode().run(
            LlmGenerateConfig(prompt="{{ input.summary }}"),
            make_context(recorder, http=http_client, node_input={"summary": "  "}),
        )

    assert "rendered empty" in str(raised.value)
    assert called["n"] == 0
