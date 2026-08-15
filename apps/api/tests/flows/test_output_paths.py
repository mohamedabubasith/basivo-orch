"""Output paths — the contract the editor's template autocomplete is built on.

Two things pinned here. First, the shape: nodes with a stable output declare
paths, nodes whose output is the author's own (code, manual trigger) declare
none — suggesting made-up paths is worse than suggesting none. Second, the
delivery: `NodeTypeRead` must carry the field, because FastAPI's
response_model filtering silently strips anything the schema does not name —
which is exactly how the first version shipped a palette with the paths
missing while every unit inspection of `palette()` looked correct.
"""

from __future__ import annotations

from basivo_orch.flows import nodes as registry
from basivo_orch.flows.schemas import NodeTypeRead


def test_stable_nodes_declare_their_paths():
    by_type = {spec["type"]: spec["output_paths"] for spec in registry.palette()}

    assert by_type["trigger.webhook"] == ["body", "headers", "query", "method"]
    assert by_type["http.request"] == ["status", "headers", "body"]
    assert "usage.cost_usd" in by_type["agent.llm"]
    assert by_type["git.ticket"] == ["url", "number"]
    assert "pr_url" in by_type["git.autofix"]
    # Author-shaped outputs: nothing to promise, so nothing suggested.
    assert by_type["code.python"] == []
    assert by_type["trigger.manual"] == []


def test_the_response_schema_carries_output_paths():
    validated = NodeTypeRead.model_validate(
        next(spec for spec in registry.palette() if spec["type"] == "trigger.webhook")
    )
    assert validated.output_paths == ["body", "headers", "query", "method"]
