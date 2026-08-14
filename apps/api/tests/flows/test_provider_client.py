"""`construct_provider` — shared by the Agent node and the model catalog.

Moved out of the Agent node once a second caller (`model_catalog`, for the
credential form's "Test connection" and the model dropdown) needed the exact
same construction logic. These tests moved with it.
"""

from __future__ import annotations

from basivo_orch.credentials.provider_client import construct_provider


def test_construct_provider_only_passes_kwargs_the_constructor_accepts():
    """Bedrock authenticates by AWS signature, not a bearer key — a stored
    credential's `api_key` must not be forced onto a constructor that has no
    such parameter."""

    class FakeBedrockLikeProvider:
        def __init__(self, region_name: str = "us-east-1") -> None:
            self.region_name = region_name

    provider = construct_provider(
        FakeBedrockLikeProvider,
        api_key="sk-should-be-ignored",
        base_url="",
        options={"region_name": "eu-west-1"},
    )
    assert provider.region_name == "eu-west-1"
    assert not hasattr(provider, "api_key")


def test_construct_provider_passes_api_key_when_accepted():
    class FakeKeyedProvider:
        def __init__(self, api_key: str = "") -> None:
            self.api_key = api_key

    provider = construct_provider(FakeKeyedProvider, api_key="sk-live-123", base_url="", options={})
    assert provider.api_key == "sk-live-123"


def test_construct_provider_passes_base_url_when_accepted():
    class FakeUrlProvider:
        def __init__(self, base_url: str = "") -> None:
            self.base_url = base_url

    provider = construct_provider(
        FakeUrlProvider, api_key="", base_url="https://gateway.example.com/v1", options={}
    )
    assert provider.base_url == "https://gateway.example.com/v1"


def test_construct_provider_ignores_empty_values():
    class FakeProvider:
        def __init__(self, api_key: str = "default", base_url: str = "default") -> None:
            self.api_key = api_key
            self.base_url = base_url

    provider = construct_provider(FakeProvider, api_key="", base_url="", options={})
    assert provider.api_key == "default"
    assert provider.base_url == "default"
