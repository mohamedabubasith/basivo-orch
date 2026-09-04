"""Branching and data shaping — the two utilities every flow ends up needing."""

from __future__ import annotations

import operator
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import TemplateError, render_value

TRUE_PORT = "true"
FALSE_PORT = "false"


def _contains(left: Any, right: Any) -> bool:
    try:
        return right in left
    except TypeError:
        return False


def _matches(left: Any, right: Any) -> bool:
    try:
        # Bounded so a pathological pattern from a flow author cannot wedge a
        # worker: re has no timeout, so the guard has to be on the input size.
        return re.search(str(right)[:500], str(left)[:10000]) is not None
    except re.error as exc:
        raise NodeError(f"Invalid regular expression: {exc}") from exc


def _to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NodeError(f"{value!r} is not a number.") from exc


def _numeric(op: Any) -> Any:
    return lambda left, right: op(_to_number(left), _to_number(right))


#: Comparisons a Condition node can make. Named rather than expression-based,
#: for the reason set out in templating.py: flow definitions are user input.
OPERATORS = {
    "equals": lambda a, b: a == b,
    "not_equals": lambda a, b: a != b,
    "greater_than": _numeric(operator.gt),
    "greater_or_equal": _numeric(operator.ge),
    "less_than": _numeric(operator.lt),
    "less_or_equal": _numeric(operator.le),
    "contains": _contains,
    "not_contains": lambda a, b: not _contains(a, b),
    "matches": _matches,
    "is_empty": lambda a, _b: a in (None, "", [], {}),
    "is_not_empty": lambda a, _b: a not in (None, "", [], {}),
    "is_true": lambda a, _b: bool(a) is True,
    "is_false": lambda a, _b: bool(a) is False,
}


class Comparison(BaseModel):
    left: Any = Field(description="Usually a {{ reference }}.")
    operator: Literal[tuple(OPERATORS)] = "equals"  # type: ignore[valid-type]
    right: Any = None


class ConditionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    comparisons: list[Comparison] = Field(min_length=1, max_length=20)
    #: How multiple comparisons combine.
    match: Literal["all", "any"] = "all"


class ConditionNode(Node):
    type = "logic.condition"
    label = "If / Else"
    description = "Send the flow down one of two branches."
    when = (
        "The next step depends on a value: a status code, a word in a message, whether a "
        "field is empty. Wire the true and false outputs to different nodes."
    )
    needs = ("A trigger before it, or any node whose output it should work on",)
    example = "Webhook -> If / Else -> Open Issue, or -> Set Variables"
    tier = 1
    category = "utility"
    ports = (TRUE_PORT, FALSE_PORT)
    config_model = ConditionConfig
    output_paths = ("result", "comparisons")

    async def run(self, config: ConditionConfig, ctx: NodeContext) -> NodeResult:
        context = ctx.template_context()
        results: list[bool] = []
        detail: list[dict[str, Any]] = []

        for comparison in config.comparisons:
            try:
                left = render_value(comparison.left, context)
                right = render_value(comparison.right, context)
            except TemplateError as exc:
                # A missing reference is a definite false, not a crash: the
                # common case is branching on a field an upstream API omitted.
                results.append(False)
                detail.append(
                    {"operator": comparison.operator, "result": False, "reason": str(exc)}
                )
                continue

            outcome = bool(OPERATORS[comparison.operator](left, right))
            results.append(outcome)
            detail.append(
                {"operator": comparison.operator, "left": left, "right": right, "result": outcome}
            )

        passed = all(results) if config.match == "all" else any(results)
        await ctx.progress(f"condition → {passed}")

        return NodeResult(
            output={"result": passed, "comparisons": detail},
            ports=[TRUE_PORT if passed else FALSE_PORT],
        )


class Assignment(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    value: Any = None


class SetVariablesConfig(BaseModel):
    model_config = {"extra": "forbid"}

    assignments: list[Assignment] = Field(min_length=1, max_length=50)
    #: When true the node outputs only these values, replacing what it received.
    replace_output: bool = False


class SetVariablesNode(Node):
    type = "data.set"
    label = "Set Variables"
    description = "Shape data and carry values to later nodes."
    when = (
        "You need to rename, pick or compute a few values so later nodes get exactly what "
        "they expect, without writing code."
    )
    needs = ("A trigger before it, or any node whose output it should work on",)
    example = "HTTP Request -> Set Variables -> AI Agent"
    tier = 1
    category = "utility"
    config_model = SetVariablesConfig

    async def run(self, config: SetVariablesConfig, ctx: NodeContext) -> NodeResult:
        context = ctx.template_context()
        assigned: dict[str, Any] = {}

        for assignment in config.assignments:
            try:
                assigned[assignment.name] = render_value(assignment.value, context)
            except TemplateError as exc:
                # Unlike Condition, a bad reference here is a real error: the
                # author asked for a value and silently substituting null would
                # push the failure into a later node where it is harder to read.
                raise NodeError(f"{assignment.name}: {exc}") from exc
            # Later assignments can build on earlier ones in the same node.
            context["vars"] = {**context.get("vars", {}), **assigned}

        output = (
            assigned
            if config.replace_output
            else {**(ctx.input or {}), **assigned}
            if isinstance(ctx.input, dict)
            else assigned
        )

        return NodeResult(output=output, variables=assigned)
