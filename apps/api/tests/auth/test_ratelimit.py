"""Rate limiting, exercised with the limiter actually switched on.

The rest of the suite runs with ``RATE_LIMIT_ENABLED=false`` so unrelated tests
are not throttled. That leaves a blind spot worth closing explicitly: SlowAPI
injects its ``X-RateLimit-*`` headers into a ``response`` argument, and a
decorated handler that does not declare one raises at request time. With the
limiter disabled the decorator is a no-op, so such a handler passes every test
and then fails on the first production request.

:func:`test_every_rate_limited_handler_accepts_a_response` checks the signatures
directly, so it holds for endpoints this module never calls.
"""

from __future__ import annotations

import inspect

import pytest

from conftest import build_app

pytestmark = pytest.mark.security


def _router_modules() -> list:
    """Every module under app.auth.routers, whichever features are enabled."""
    import importlib
    import pkgutil

    import basivo_orch.auth.routers as package

    return [
        importlib.import_module(f"{package.__name__}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]


def test_every_rate_limited_handler_accepts_a_response() -> None:
    """A limited handler must declare ``response: Response``.

    Without it SlowAPI raises ``parameter `response` must be an instance of
    starlette.responses.Response`` and the endpoint returns 500 — but only once
    rate limiting is enabled, which is to say only in production.
    """
    offenders: list[str] = []

    for module in _router_modules():
        source = inspect.getsource(module)
        for block in source.split("@limiter.limit")[1:]:
            signature = block.split(") ->")[0]
            handler = block.split("async def ", 1)[-1].split("(")[0]
            if "response: Response" not in signature:
                offenders.append(f"{module.__name__}.{handler}")

    assert not offenders, (
        "these rate-limited handlers must accept `response: Response`, "
        f"or they will 500 whenever rate limiting is on: {sorted(set(offenders))}"
    )


def test_the_app_builds_with_rate_limiting_on() -> None:
    """Smoke check that route registration itself is limiter-safe."""
    assert build_app() is not None


async def test_login_is_rate_limited(client, user, monkeypatch) -> None:
    """The documented login limit must actually fire."""
    from basivo_orch.auth.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("LOGIN_RATE_LIMIT", "3/minute")

    from basivo_orch.auth.security import ratelimit

    ratelimit.limiter.enabled = True
    ratelimit.limiter.reset()

    statuses = []
    for _ in range(6):
        response = await client.post(
            "/auth/login",
            data={"username": user.email, "password": "definitely-not-the-password"},
        )
        statuses.append(response.status_code)

    ratelimit.limiter.enabled = False
    ratelimit.limiter.reset()
    get_settings.cache_clear()

    assert 429 in statuses, f"login should throttle, got {statuses}"


async def test_rate_limited_endpoint_does_not_error(client, monkeypatch) -> None:
    """With limiting on, a limited endpoint must return its own status, not 500."""
    from basivo_orch.auth.security import ratelimit

    ratelimit.limiter.enabled = True
    ratelimit.limiter.reset()

    response = await client.post("/auth/forgot-password", json={"email": "nobody@example.com"})

    ratelimit.limiter.enabled = False
    ratelimit.limiter.reset()

    assert response.status_code != 500, response.text
    assert response.status_code == 202
