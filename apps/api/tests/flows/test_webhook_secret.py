"""The webhook trigger's Require-signature switch — now attached to something.

The switch shipped as a bool that nothing read: its docstring promised an
edge check that did not exist. These tests pin the two halves that make it
real — the config refuses the switch without a secret, and the edge check
refuses calls without the matching header — so it cannot quietly become
decorative again.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from basivo_orch.flows.graph import Graph
from basivo_orch.flows.nodes.triggers import WebhookTriggerConfig
from basivo_orch.flows.router import _verify_webhook_secret


def make_graph(config: dict) -> Graph:
    return Graph.model_validate(
        {
            "nodes": [{"id": "hook", "type": "trigger.webhook", "config": config, "position": {}}],
            "edges": [],
        }
    )


def test_the_switch_refuses_to_exist_without_a_secret():
    with pytest.raises(ValidationError, match="no secret is set"):
        WebhookTriggerConfig(require_signature=True)


def test_the_switch_accepts_a_secret():
    config = WebhookTriggerConfig(require_signature=True, secret="whsec_abc")  # noqa: S106 — test fixture
    assert config.secret == "whsec_abc"  # noqa: S105 — test fixture


def test_edge_check_passes_when_signature_not_required():
    _verify_webhook_secret(make_graph({}), presented=None)


def test_edge_check_rejects_a_missing_header():
    graph = make_graph({"require_signature": True, "secret": "whsec_abc"})
    with pytest.raises(HTTPException) as raised:
        _verify_webhook_secret(graph, presented=None)
    assert raised.value.status_code == 401


def test_edge_check_rejects_a_wrong_secret():
    graph = make_graph({"require_signature": True, "secret": "whsec_abc"})
    with pytest.raises(HTTPException) as raised:
        _verify_webhook_secret(graph, presented="whsec_WRONG")
    assert raised.value.status_code == 401


def test_edge_check_accepts_the_right_secret():
    graph = make_graph({"require_signature": True, "secret": "whsec_abc"})
    _verify_webhook_secret(graph, presented="whsec_abc")


def test_non_webhook_flows_are_untouched():
    graph = Graph.model_validate(
        {
            "nodes": [{"id": "m", "type": "trigger.manual", "config": {}, "position": {}}],
            "edges": [],
        }
    )
    _verify_webhook_secret(graph, presented=None)
