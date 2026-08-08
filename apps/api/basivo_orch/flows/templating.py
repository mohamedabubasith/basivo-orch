"""Reference resolution for node configuration.

Node config contains references to earlier results: `{{ nodes.fetch.output.id }}`,
`{{ trigger.body.email }}`, `{{ vars.retries }}`.

**This is deliberately not an expression language.** The obvious implementation
is Jinja, or `eval` on the inside of the braces, and both hand every author of
a flow the ability to run arbitrary Python inside the orchestrator — in a
product whose whole purpose is executing definitions written by users, on
shared infrastructure, against configured credentials. There is no sandbox
here that would make that safe.

So a reference is a path: dotted keys and integer indices, resolved against
plain data. No calls, no operators, no attribute access on Python objects. If
that turns out to be too little, the answer is a named, audited helper
(`{{ upper(vars.name) }}` implemented in `FILTERS`), not a general evaluator.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Matches `{{ ... }}` with optional surrounding whitespace.
REFERENCE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

#: A reference that makes up the *entire* string, so the resolved value keeps
#: its own type instead of being stringified.
WHOLE_REFERENCE = re.compile(r"^\{\{\s*([^}]+?)\s*\}\}$")

MAX_DEPTH = 20


class TemplateError(Exception):
    """A reference that cannot be resolved against the current context."""


def _lookup(context: dict[str, Any], path: str) -> Any:
    """Walk a dotted path over plain data.

    Only dict keys and list indices. Anything else is a lookup failure rather
    than an attribute access, which is what keeps `{{ x.__class__ }}` from
    being a foothold.
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise TemplateError("Empty reference.")

    current: Any = context
    walked: list[str] = []
    for part in parts:
        walked.append(part)
        if isinstance(current, dict):
            if part not in current:
                raise TemplateError(f"{'.'.join(walked)} is not available.")
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                index = int(part)
            except ValueError:
                raise TemplateError(
                    f"{'.'.join(walked[:-1])} is a list; use a number, not {part!r}."
                ) from None
            if not -len(current) <= index < len(current):
                raise TemplateError(f"{'.'.join(walked[:-1])} has no item {index}.")
            current = current[index]
        else:
            raise TemplateError(f"{'.'.join(walked[:-1])} has no field {part!r}.")
    return current


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, default=str)


def render_value(value: Any, context: dict[str, Any], *, depth: int = 0) -> Any:
    """Resolve references anywhere inside a config value.

    Strings that are exactly one reference return the referenced value with its
    type intact — `{{ trigger.body.count }}` yields the number 3, not "3", so a
    downstream node comparing it numerically works.
    """
    if depth > MAX_DEPTH:
        raise TemplateError("Configuration is nested too deeply.")

    if isinstance(value, str):
        if whole := WHOLE_REFERENCE.match(value):
            return _lookup(context, whole.group(1))
        if "{{" not in value:
            return value
        return REFERENCE.sub(lambda m: _stringify(_lookup(context, m.group(1))), value)

    if isinstance(value, dict):
        return {k: render_value(v, context, depth=depth + 1) for k, v in value.items()}

    if isinstance(value, list):
        return [render_value(v, context, depth=depth + 1) for v in value]

    return value


def references_in(value: Any) -> set[str]:
    """Every path a config refers to. Used to explain a failure without running."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(m.strip() for m in REFERENCE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= references_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= references_in(item)
    return found
