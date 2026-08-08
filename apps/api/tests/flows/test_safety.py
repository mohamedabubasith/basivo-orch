"""Tests for the two places a workflow engine usually grows a hole.

Both are marked `security`: each encodes a property, not a behaviour, and each
fails if the property is lost rather than if an implementation detail changes.
"""

from __future__ import annotations

import pytest

from basivo_orch.flows.nodes.http import BlockedRequest, assert_public_url
from basivo_orch.flows.templating import TemplateError, render_value

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # The one that matters: cloud instance metadata. Reachable from inside
        # every EC2 and GCE box, and it serves credentials to anything that asks.
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8000/health",
        "http://localhost:5432",
        "http://[::1]:6379",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "http://0.0.0.0:8000",
    ],
)
def test_internal_addresses_are_refused(url: str) -> None:
    """A flow author must not be able to point a node at our own network."""
    with pytest.raises(BlockedRequest):
        assert_public_url(url)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://127.0.0.1:6379/_INFO", "ftp://example.com/x", "//example.com"],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    """`file://` reads the disk and `gopher://` can speak Redis's protocol."""
    with pytest.raises(BlockedRequest):
        assert_public_url(url)


def test_a_name_that_does_not_resolve_is_not_allowed_through() -> None:
    """`metadata.google.internal` resolves to 169.254.169.254 on GCE and to
    nothing here. Either way the request must not proceed — the failure modes
    differ (blocked vs. unresolvable) but neither is permissive."""
    from basivo_orch.flows.nodes.base import NodeError

    with pytest.raises(NodeError):
        assert_public_url("http://metadata.google.internal/computeMetadata/v1/")


def test_public_addresses_are_allowed() -> None:
    assert_public_url("https://example.com/webhook")
    assert_public_url("http://93.184.216.34/")


def test_the_error_does_not_reveal_the_resolved_address() -> None:
    """The message must not become an internal-network scanner.

    Reporting "resolved to 10.0.3.17" would let anyone map our private space
    one hostname at a time.
    """
    # Uses a hostname, not a literal: echoing back an address the caller typed
    # tells them nothing, but revealing what a *name* resolved to is the leak.
    with pytest.raises(BlockedRequest) as caught:
        assert_public_url("http://localhost:5432/")
    assert "127.0.0.1" not in str(caught.value)
    assert "::1" not in str(caught.value)


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------

CONTEXT = {
    "trigger": {"payload": {"email": "a@b.com", "count": 3, "tags": ["x", "y"]}},
    "nodes": {"fetch": {"status": 200, "body": {"id": 7}}},
    "vars": {"greeting": "hello"},
    "input": {"value": 1},
}


def test_a_whole_reference_keeps_its_type() -> None:
    """`{{ ... }}` alone yields the value, so a number stays comparable."""
    assert render_value("{{ trigger.payload.count }}", CONTEXT) == 3
    assert render_value("{{ trigger.payload.tags }}", CONTEXT) == ["x", "y"]


def test_an_embedded_reference_interpolates() -> None:
    assert render_value("Hi {{ trigger.payload.email }}!", CONTEXT) == "Hi a@b.com!"


def test_list_indices_resolve() -> None:
    assert render_value("{{ trigger.payload.tags.1 }}", CONTEXT) == "y"


def test_nested_config_is_rendered() -> None:
    rendered = render_value(
        {"headers": {"X-Id": "{{ nodes.fetch.body.id }}"}, "items": ["{{ vars.greeting }}"]},
        CONTEXT,
    )
    # 7 stays an int: a lone reference keeps its type so downstream numeric
    # comparisons work. The HTTP node stringifies header values itself.
    assert rendered == {"headers": {"X-Id": 7}, "items": ["hello"]}


@pytest.mark.parametrize(
    "expression",
    [
        # If any of these ever resolve, the templating layer has become an
        # expression evaluator and a flow definition is remote code execution.
        "{{ __import__('os').system('id') }}",
        "{{ ().__class__.__bases__ }}",
        "{{ trigger.__class__ }}",
        "{{ vars.__dict__ }}",
        "{{ 1 + 1 }}",
        "{{ open('/etc/passwd') }}",
    ],
)
def test_expressions_are_not_evaluated(expression: str) -> None:
    """Only data paths resolve. Everything else is a lookup failure."""
    with pytest.raises(TemplateError):
        render_value(expression, CONTEXT)


def test_missing_reference_names_the_path() -> None:
    with pytest.raises(TemplateError) as caught:
        render_value("{{ nodes.fetch.body.missing }}", CONTEXT)
    assert "nodes.fetch.body.missing" in str(caught.value)


def test_dunder_lookup_on_a_dict_is_a_plain_miss() -> None:
    """A dict key called `__class__` would be data, not the Python attribute."""
    with pytest.raises(TemplateError):
        render_value("{{ nodes.__class__ }}", CONTEXT)
