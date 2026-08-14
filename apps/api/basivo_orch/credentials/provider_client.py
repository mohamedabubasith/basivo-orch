"""Building a pydantic-ai `Provider` from a decrypted credential.

Two callers need exactly this: the Agent node, to actually run a model, and
`model_catalog`, to fetch a live model list and prove a key works before it is
saved. Constructing it in one place means both stay consistent with whatever a
given provider's constructor actually accepts, rather than one of the two
drifting out of sync with pydantic-ai's own constructors over time.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic_ai.providers import Provider


def construct_provider(
    provider_cls: type[Provider[Any]], *, api_key: str, base_url: str, options: dict[str, Any]
) -> Provider[Any]:
    """Build a `Provider` from what a stored credential actually has.

    Constructors differ by provider — Bedrock authenticates by AWS signature,
    not a bearer key, and several accept a `base_url` while others don't — so
    kwargs are filtered to what each constructor declares rather than assumed
    uniform. A key that has nowhere to go (an unrecognised constructor
    parameter) is silently dropped by this filter; that is preferable to a
    `TypeError` that names the field but not the fix, and a caller that fails
    on its first real request because auth was never applied says so
    unambiguously through the provider SDK's own error.
    """
    accepted = set(inspect.signature(provider_cls.__init__).parameters)
    kwargs: dict[str, Any] = {}
    if api_key and "api_key" in accepted:
        kwargs["api_key"] = api_key
    if base_url and "base_url" in accepted:
        kwargs["base_url"] = base_url
    for key, value in options.items():
        if key in accepted:
            kwargs[key] = value
    return provider_cls(**kwargs)
