"""Skill payloads, and the `SKILL.md` parser.

The parser exists so this product is not a walled garden: Anthropic's skill
format is a plain markdown file with YAML frontmatter, people already have
folders of them, and asking someone to retype one into a web form to use it
here would be a poor trade. Import takes the file as-is.

Parsing is deliberately narrow — `name` and `description` out of the
frontmatter, everything after it as the body. A full YAML parse is not needed
and would be a liability: this input arrives over HTTP, and `yaml.safe_load`
is only safe in the sense of not constructing objects.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Lowercase, hyphenated. The model passes this as a tool argument, so spaces
#: and capitals are avoidable friction rather than a matter of taste.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")

#: A skill's body has to be readable in full once loaded. Beyond this it stops
#: being a procedure and becomes a document that belongs in `resources`, where
#: it can be read a piece at a time.
MAX_INSTRUCTIONS = 40_000
MAX_RESOURCE = 200_000


class SkillResource(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_RESOURCE)


class SkillWrite(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(min_length=10, max_length=500)
    instructions: str = Field(min_length=1, max_length=MAX_INSTRUCTIONS)
    resources: list[SkillResource] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def _name_is_a_tool_argument(cls, value: str) -> str:
        candidate = value.strip().lower().replace(" ", "-").replace("_", "-")
        candidate = re.sub(r"-+", "-", candidate)
        if not NAME_PATTERN.match(candidate):
            raise ValueError(
                "A skill name must be lowercase letters, numbers and hyphens — "
                "it is passed to the model as a tool argument."
            )
        return candidate


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    instructions: str
    resources: list[dict[str, Any]]
    load_count: int
    created_at: datetime
    updated_at: datetime


class SkillSummary(BaseModel):
    """What a picker needs. Omits the body, which can be 40k characters."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    load_count: int
    updated_at: datetime
    #: So the UI can say "3 files, 1.2k words" without shipping either.
    resource_count: int = 0
    instruction_chars: int = 0


class SkillImport(BaseModel):
    """A `SKILL.md` file, verbatim."""

    content: str = Field(min_length=10, max_length=MAX_INSTRUCTIONS + 2000)
    #: Optional override when the file's frontmatter has no name.
    name: str = Field(default="", max_length=80)


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_-]+):\s*(.*)$")


def parse_skill_md(content: str, *, fallback_name: str = "") -> SkillWrite:
    """Turn a `SKILL.md` into a skill, or raise `ValueError` saying why.

    Frontmatter values may be quoted, plain, or folded across following
    indented lines — all three appear in skills people actually write, and a
    parser that only handles the first would reject most real files.
    """
    text = content.replace("\r\n", "\n").strip()
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(
            "This file has no frontmatter. A SKILL.md starts with a --- block "
            "containing at least 'name' and 'description'."
        )

    fields: dict[str, str] = {}
    key: str | None = None
    for line in match.group(1).split("\n"):
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and key:
            # A folded continuation of the previous value.
            fields[key] = f"{fields[key]} {line.strip()}".strip()
            continue
        if found := _FIELD.match(line):
            key = found.group(1).strip().lower()
            fields[key] = found.group(2).strip().strip("'\"")

    body = match.group(2).strip()
    name = fields.get("name") or fallback_name
    description = fields.get("description", "")

    if not name:
        raise ValueError("The frontmatter has no 'name', and none was supplied.")
    if len(description) < 10:
        raise ValueError(
            "The frontmatter needs a 'description' of at least 10 characters. It is "
            "the only part the model reads before deciding whether to open the skill, "
            "so it should say when the skill applies."
        )
    if not body:
        raise ValueError("There are no instructions after the frontmatter block.")

    return SkillWrite(
        name=name, description=description, instructions=body[:MAX_INSTRUCTIONS], resources=[]
    )


def to_skill_md(name: str, description: str, instructions: str) -> str:
    """The inverse, so a skill authored here can be exported and version
    controlled next to the code it describes."""
    escaped = description.replace("\n", " ").strip()
    return f"---\nname: {name}\ndescription: {escaped}\n---\n\n{instructions.strip()}\n"
