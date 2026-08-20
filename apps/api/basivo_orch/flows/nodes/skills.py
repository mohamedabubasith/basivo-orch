"""Skills at run time: a catalogue in the prompt, bodies behind a tool.

This is the mechanism that makes a library of skills affordable, and it is
worth being precise about why it is built this way.

The naive approach is to paste every selected skill's instructions into the
system prompt. That works for one skill and degrades badly for five: the run
pays for all of them on every model call, whether relevant or not, and the
actual request ends up buried behind several thousand words of procedure the
model was not asked to follow this time.

So the prompt carries only a **catalogue** — one line per skill, name and
description — plus an instruction to open the relevant one first. The model
calls `load_skill("refund-policy")` and *then* has the procedure. Cost scales
with the skills used, not the skills available, and the run log records which
were offered and which were opened, which is the only way to answer "why did
it not follow the escalation policy" afterwards.

Two guards that are not optional:

**A load budget.** Skill bodies are up to 40k characters each and a model that
loads all five has quietly built a prompt nothing bounded. `SkillBudget` caps
the total characters admitted per run and says so in the tool result, so the
model is told it has hit a limit rather than silently getting less than it
asked for.

**Skill text is content, not instruction.** A skill body is written by a
workspace member, so it is trusted in the way a system prompt is — but it is
returned as a *tool result*, never spliced into the system prompt, so the model
reads it as reference material provided during the conversation. That keeps the
node's own framing (its system prompt, its limits) above anything a skill says.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from basivo_orch.flows.nodes.base import NodeContext

#: How many characters of skill text one run may pull in, across every load.
#: Generous enough for three or four real procedures; small enough that no
#: single run can turn the library into its context window.
DEFAULT_SKILL_BUDGET = 60_000


@dataclass(slots=True)
class LoadedSkill:
    """A skill as the node received it from the engine."""

    id: str
    name: str
    description: str
    instructions: str
    resources: list[dict[str, Any]] = field(default_factory=list)

    def resource(self, name: str) -> dict[str, Any] | None:
        wanted = name.strip().lower()
        for entry in self.resources:
            if str(entry.get("name", "")).strip().lower() == wanted:
                return entry
        return None


@dataclass(slots=True)
class SkillBudget:
    """Characters of skill text this run may still admit."""

    limit: int = DEFAULT_SKILL_BUDGET
    spent: int = 0
    loaded: list[str] = field(default_factory=list)

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)


def catalogue(skills: list[LoadedSkill]) -> str:
    """The block appended to the system prompt.

    Names are quoted exactly as the tool expects them: a model that infers
    `Refund Policy` from a heading and calls the tool with that will get an
    error it has to recover from, and the fix is to never show it the other
    form in the first place.
    """
    if not skills:
        return ""
    lines = [f'- "{skill.name}" — {skill.description}' for skill in skills]
    return (
        "\n\n## Skills available to you\n\n"
        "These are procedures this workspace has written down. Each line is a "
        "skill's name and when it applies.\n\n"
        + "\n".join(lines)
        + "\n\nWhen one of them applies to the request, call `load_skill` with its "
        "name BEFORE you answer, and then follow what it says. Do not guess at a "
        "procedure you have not opened, and do not open skills that are not "
        "relevant — each one costs the run."
    )


def skill_tools(
    ctx: NodeContext,
    skills: list[LoadedSkill],
    *,
    budget: SkillBudget,
) -> list[Any]:
    """`load_skill` and, when any skill bundles files, `read_skill_file`."""
    from basivo_orch.flows.nodes.agent_runtime import build_tool

    if not skills:
        return []

    by_name = {skill.name: skill for skill in skills}
    known = ", ".join(sorted(by_name))

    async def load_skill(name: str) -> str:
        wanted = str(name or "").strip().lower()
        skill = by_name.get(wanted)
        if skill is None:
            await ctx.step("skill.unknown", {"asked_for": wanted})
            # Naming the real options turns a dead end into a recoverable turn.
            return f"There is no skill called {wanted!r}. Available skills: {known}."

        if wanted in budget.loaded:
            # Re-reading the same skill is a loop, not new information.
            return (
                f"You already loaded {wanted!r} earlier in this run — scroll back to it "
                "rather than loading it again."
            )

        body = skill.instructions
        if len(body) > budget.remaining():
            await ctx.step(
                "skill.budget_exceeded",
                {"skill": wanted, "chars": len(body), "remaining": budget.remaining()},
            )
            return (
                f"The skill {wanted!r} is {len(body)} characters and this run has only "
                f"{budget.remaining()} left of its skill budget. Work from what you have "
                "already loaded, and say in your answer that you could not open it."
            )

        budget.spent += len(body)
        budget.loaded.append(wanted)
        files = [str(entry.get("name", "")) for entry in (skill.resources or [])]
        await ctx.step(
            "skill.loaded",
            {
                "skill": wanted,
                "skill_id": skill.id,
                "chars": len(body),
                "files": files,
                "budget_left": budget.remaining(),
            },
        )
        if ctx.record_skill_load is not None:
            await ctx.record_skill_load(skill.id)

        header = f"# Skill: {skill.name}\n\n{skill.description}\n\n"
        footer = (
            "\n\nBundled files you can read with `read_skill_file`: " + ", ".join(files)
            if files
            else ""
        )
        return header + body + footer

    async def read_skill_file(skill: str, name: str) -> str:
        record = by_name.get(str(skill or "").strip().lower())
        if record is None:
            return f"There is no skill called {skill!r}. Available skills: {known}."
        entry = record.resource(str(name or ""))
        if entry is None:
            have = ", ".join(str(e.get("name", "")) for e in record.resources) or "none"
            return f"{record.name} has no file called {name!r}. It bundles: {have}."

        content = str(entry.get("content", ""))
        if len(content) > budget.remaining():
            # Truncation is stated in the text the model reads, so it knows the
            # file is incomplete rather than believing it ended there.
            content = content[: budget.remaining()] + "\n\n[truncated: skill budget reached]"
        budget.spent += len(content)
        await ctx.step(
            "skill.file_read",
            {"skill": record.name, "file": entry.get("name"), "chars": len(content)},
        )
        return f"# {record.name} / {entry.get('name')}\n\n{content}"

    tools = [
        build_tool(
            name="load_skill",
            description=(
                "Open one of this workspace's skills and read its full instructions. "
                "Call this before doing work a skill covers. Available: " + known
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill's exact name, e.g. " + sorted(by_name)[0],
                    }
                },
                "required": ["name"],
            },
            execute=load_skill,
        )
    ]

    if any(skill.resources for skill in skills):
        tools.append(
            build_tool(
                name="read_skill_file",
                description=(
                    "Read one file bundled with a skill. The file names are listed at the "
                    "end of the skill's instructions once you have loaded it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "description": "The skill's name."},
                        "name": {"type": "string", "description": "The file's name."},
                    },
                    "required": ["skill", "name"],
                },
                execute=read_skill_file,
            )
        )
    return tools
