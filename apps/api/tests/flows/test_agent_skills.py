"""Agent skills, and specifically the progressive part.

The claim worth testing is not "the instructions reach the model" — pasting
them into the prompt would do that. It is that they reach the model *only when
it asks*, and that the catalogue is what it chooses from. So these tests read
the system prompt the model actually received, and assert the body is absent
until a `load_skill` call happens.

The parser gets its own tests because the input is a file someone wrote by
hand, which is the least predictable input in the product.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from basivo_orch.flows.nodes.agent import AgentConfig, AgentNode
from basivo_orch.flows.nodes.base import NodeContext
from basivo_orch.flows.nodes.skills import (
    DEFAULT_SKILL_BUDGET,
    LoadedSkill,
    SkillBudget,
    catalogue,
    skill_tools,
)
from basivo_orch.skills.schemas import SkillWrite, parse_skill_md, to_skill_md
from tests.flows.fakes import FakeChatModel, says, tool_call, turn_number

REFUNDS = LoadedSkill(
    id=str(uuid.uuid4()),
    name="refund-policy",
    description="Use when a customer asks for money back, including chargebacks.",
    instructions="# Refunds\n\n1. Check the order date.\n2. Under 30 days: refund in full.",
    resources=[{"name": "exceptions.md", "content": "Enterprise contracts: ask legal."}],
)
ESCALATION = LoadedSkill(
    id=str(uuid.uuid4()),
    name="escalation",
    description="Use when the customer is threatening to leave or has asked for a manager.",
    instructions="# Escalation\n\nPage the duty lead in #support-urgent.",
)


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.steps]

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(
    recorder: _Recorder,
    *,
    http: httpx.AsyncClient,
    skills: list[LoadedSkill] | None = None,
    counted: list[str] | None = None,
) -> NodeContext:
    async def resolve_credential(_credential_id: str):
        return None

    async def load_skills(ids: list[str]) -> list[LoadedSkill]:
        available = {skill.id: skill for skill in (skills or [])}
        return [available[i] for i in ids if i in available]

    async def record_skill_load(skill_id: str) -> None:
        if counted is not None:
            counted.append(skill_id)

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="agent_1",
        node_name="Agent",
        attempt=1,
        input={"text": "I want my money back"},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
        load_skills=load_skills,
        record_skill_load=record_skill_load,
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


def scripted(monkeypatch, seen: list, respond):
    async def build(_ctx, **_kwargs):
        def wrapper(messages):
            seen.append(list(messages))
            return respond(messages)

        return FakeChatModel(respond=wrapper)

    monkeypatch.setattr("basivo_orch.flows.nodes.agent.build_chat_model", build)


# ---------------------------------------------------------------------------
# The progressive part
# ---------------------------------------------------------------------------


async def test_the_body_arrives_only_after_the_agent_asks(monkeypatch, http_client):
    """The whole design in one test.

    Turn one: the model sees the catalogue and no procedure. It calls
    `load_skill`. Turn two: the procedure is in the conversation, as a tool
    result rather than as system text.
    """
    seen: list = []

    def respond(messages):
        if turn_number(messages) == 0:
            return tool_call("load_skill", {"name": "refund-policy"}, call_id="s1")
        return says("Refunded in full — the order is 4 days old.")

    scripted(monkeypatch, seen, respond)
    recorder = _Recorder()
    counted: list[str] = []
    config = AgentConfig(prompt="{{ input.text }}", skills=[REFUNDS.id, ESCALATION.id])

    await AgentNode().run(
        config,
        make_context(recorder, http=http_client, skills=[REFUNDS, ESCALATION], counted=counted),
    )

    first_system = str(seen[0][0].content)
    # The catalogue is there — both skills, by exact tool name.
    assert '"refund-policy"' in first_system and '"escalation"' in first_system
    assert "Use when a customer asks for money back" in first_system
    # The procedures are NOT.
    assert "Check the order date" not in first_system
    assert "Page the duty lead" not in str(seen[0])

    # After the call, the loaded skill's body is in the conversation.
    everything = " ".join(str(m.content) for m in seen[1])
    assert "Check the order date" in everything
    # And only the one it asked for.
    assert "Page the duty lead" not in everything

    assert recorder.data_for("skill.offered")[0]["skills"] == ["refund-policy", "escalation"]
    loaded = recorder.data_for("skill.loaded")[0]
    assert loaded["skill"] == "refund-policy"
    assert loaded["files"] == ["exceptions.md"]
    assert counted == [REFUNDS.id], "a load should count towards the library's usage"


async def test_an_agent_with_no_skills_gets_no_skill_tools(monkeypatch, http_client):
    """An unusable `load_skill` in the tool list is a trap, not a feature."""
    seen: list = []
    scripted(monkeypatch, seen, lambda messages: says("done"))
    recorder = _Recorder()

    await AgentNode().run(
        AgentConfig(prompt="hi"), make_context(recorder, http=http_client, skills=[REFUNDS])
    )

    assert "skill.offered" not in recorder.kinds()
    assert "Skills available to you" not in str(seen[0][0].content)


async def test_a_deleted_skill_is_skipped_and_logged(monkeypatch, http_client):
    """A library edit must not take down every flow that referenced it."""
    seen: list = []
    scripted(monkeypatch, seen, lambda messages: says("ok"))
    recorder = _Recorder()
    config = AgentConfig(prompt="hi", skills=[REFUNDS.id, str(uuid.uuid4())])

    result = await AgentNode().run(
        config, make_context(recorder, http=http_client, skills=[REFUNDS])
    )

    assert result.output["text"] == "ok"
    missing = recorder.data_for("skill.missing")[0]
    assert missing == {
        "expected": 2,
        "found": 1,
        "note": "Skills removed from the library are skipped.",
    }
    assert recorder.data_for("skill.offered")[0]["skills"] == ["refund-policy"]


async def test_asking_for_a_skill_that_does_not_exist_lists_the_real_ones(monkeypatch, http_client):
    """A dead end the model can recover from, rather than a failed run."""
    seen: list = []

    def respond(messages):
        if turn_number(messages) == 0:
            return tool_call("load_skill", {"name": "Refund Policy"}, call_id="s1")
        return says("recovered")

    scripted(monkeypatch, seen, respond)
    recorder = _Recorder()

    await AgentNode().run(
        AgentConfig(prompt="hi", skills=[REFUNDS.id]),
        make_context(recorder, http=http_client, skills=[REFUNDS]),
    )

    reply = " ".join(str(m.content) for m in seen[1])
    assert "no skill called 'refund policy'" in reply.lower()
    assert "refund-policy" in reply, "the error must name the real options"
    assert recorder.data_for("skill.unknown")[0]["asked_for"] == "refund policy"


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


async def test_the_budget_refuses_a_load_instead_of_blowing_the_context(http_client):
    """Told, not truncated: the model needs to know it is working blind."""
    recorder = _Recorder()
    big = LoadedSkill(
        id=str(uuid.uuid4()),
        name="big",
        description="A very long procedure.",
        instructions="x" * 5000,
    )
    budget = SkillBudget(limit=1000)
    tools = skill_tools(make_context(recorder, http=http_client), [big], budget=budget)

    answer = await tools[0].coroutine(name="big")

    assert "skill budget" in answer
    assert "could not open it" in answer
    assert budget.spent == 0
    assert recorder.data_for("skill.budget_exceeded")[0]["chars"] == 5000


async def test_loading_the_same_skill_twice_is_refused(http_client):
    """Re-reading is a loop; each repeat would also be charged to the budget."""
    recorder = _Recorder()
    budget = SkillBudget()
    tools = skill_tools(make_context(recorder, http=http_client), [REFUNDS], budget=budget)

    first = await tools[0].coroutine(name="refund-policy")
    second = await tools[0].coroutine(name="refund-policy")

    assert "Check the order date" in first
    assert "already loaded" in second
    assert budget.spent == len(REFUNDS.instructions), "the repeat must not be charged twice"


async def test_bundled_files_are_read_one_at_a_time(http_client):
    """The reason `resources` exists: a long reference should not ride along
    with every load of the skill that mentions it."""
    recorder = _Recorder()
    ctx = make_context(recorder, http=http_client)
    tools = skill_tools(ctx, [REFUNDS], budget=SkillBudget())
    assert [tool.name for tool in tools] == ["load_skill", "read_skill_file"]

    body = await tools[0].coroutine(name="refund-policy")
    assert "Enterprise contracts" not in body, "the file is listed, not inlined"
    assert "exceptions.md" in body

    content = await tools[1].coroutine(skill="refund-policy", name="exceptions.md")
    assert "Enterprise contracts: ask legal." in content
    assert recorder.data_for("skill.file_read")[0]["file"] == "exceptions.md"

    missing = await tools[1].coroutine(skill="refund-policy", name="nope.md")
    assert "no file called 'nope.md'" in missing and "exceptions.md" in missing


def test_no_file_tool_when_no_skill_bundles_files(http_client):
    """One fewer tool to choose wrongly between."""

    class _Ctx:
        pass

    tools = skill_tools(_Ctx(), [ESCALATION], budget=SkillBudget())  # type: ignore[arg-type]
    assert [tool.name for tool in tools] == ["load_skill"]


def test_the_catalogue_names_skills_exactly_as_the_tool_expects():
    """A heading-cased name in the prompt produces a failed tool call."""
    text = catalogue([REFUNDS, ESCALATION])
    assert '- "refund-policy" — Use when a customer asks' in text
    assert "Refund Policy" not in text
    assert catalogue([]) == "", "no skills, no prompt weight at all"
    assert DEFAULT_SKILL_BUDGET >= 10_000


# ---------------------------------------------------------------------------
# SKILL.md
# ---------------------------------------------------------------------------


def test_a_claude_skill_file_imports_unchanged():
    """The point of the format: an existing skill folder is usable here."""
    parsed = parse_skill_md(
        """---
name: pdf-forms
description: Use when the user needs to fill in a PDF form or extract its fields.
license: Apache-2.0
---

# PDF forms

Run `python scripts/fill.py` with the field map.
"""
    )
    assert parsed.name == "pdf-forms"
    assert parsed.description.startswith("Use when the user needs")
    assert parsed.instructions.startswith("# PDF forms")
    assert "license" not in parsed.instructions, "frontmatter must not leak into the body"


def test_a_folded_description_is_joined():
    """Real files wrap long descriptions across indented lines."""
    parsed = parse_skill_md(
        """---
name: incident-review
description: Use after any production incident, when writing
  the postmortem or deciding who to notify.
---

Body.
"""
    )
    assert parsed.description == (
        "Use after any production incident, when writing the postmortem or deciding who to notify."
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# Just markdown\n\nNo frontmatter here.", "no frontmatter"),
        ("---\nname: x\n---\n\nBody.", "description"),
        ("---\nname: x\ndescription: long enough to pass\n---\n", "no instructions"),
    ],
)
def test_a_bad_skill_file_says_what_is_wrong(content, expected):
    """Import is a paste-a-file flow, so the error has to be actionable."""
    with pytest.raises(ValueError, match=expected):
        parse_skill_md(content)


def test_export_round_trips():
    """A skill authored here can live in a repository next to its code."""
    original = SkillWrite(
        name="release-checks",
        description="Use before tagging a release, to run the pre-flight checks.",
        instructions="1. Tests green.\n2. Migrations applied.",
    )
    again = parse_skill_md(to_skill_md(original.name, original.description, original.instructions))
    assert (again.name, again.description, again.instructions) == (
        original.name,
        original.description,
        original.instructions,
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [("Refund Policy", "refund-policy"), ("customer_support", "customer-support")],
)
def test_a_name_is_normalised_to_something_a_model_can_type(given, expected):
    assert SkillWrite(name=given, description="A description of when.", instructions="x").name == (
        expected
    )


def test_a_name_that_cannot_be_normalised_is_rejected():
    with pytest.raises(ValueError, match="lowercase letters"):
        SkillWrite(name="!!!", description="A description of when.", instructions="x")
