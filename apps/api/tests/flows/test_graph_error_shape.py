"""The shape of a rejected-graph error.

`HTTPException(detail=...)` is wrapped by FastAPI inside its own top-level
`{"detail": ...}` envelope — so whatever `_graph_error` passes as `detail`
becomes `response.json()["detail"]`, not the whole body. An earlier version
named its own key `"detail"` too, producing `{"detail": {"detail": "...",
"problems": [...]}}`. The frontend's error parser looked for a string or a
`.reason` field and found neither, so every rejected publish or test run
surfaced as the bare HTTP phrase "Unprocessable Entity" — a real bug, caught
only by looking at what a browser actually rendered, not by any prior test.
This pins the fixed shape so a future refactor can't silently reintroduce it.
"""

from __future__ import annotations

from basivo_orch.flows.graph import GraphError
from basivo_orch.flows.router import _graph_error


def test_graph_error_is_not_doubly_nested():
    exc = _graph_error(GraphError(["Node 'x' is misconfigured: url"]))

    assert exc.status_code == 422
    assert isinstance(exc.detail, dict)
    # The bug: an earlier version had `exc.detail == {"detail": "...", "problems": [...]}`,
    # which FastAPI then wraps again — the frontend saw `data.detail.detail`,
    # not `data.detail.message`, and had no code path for it.
    assert "detail" not in exc.detail
    assert exc.detail["message"] == "This flow cannot run yet."
    assert exc.detail["problems"] == ["Node 'x' is misconfigured: url"]
