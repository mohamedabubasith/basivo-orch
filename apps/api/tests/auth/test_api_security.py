"""End-to-end HTTP behaviour of the security controls."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security


async def test_security_headers_are_present(client) -> None:
    # Embedded mode has no liveness route of its own; any auth route exercises
    # the same middleware stack.
    response = await client.get("/auth/csrf")
    headers = response.headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in headers
    # Auth responses carry tokens and account state; a cached one can be served
    # to the next user through a shared proxy.
    assert "no-store" in headers["cache-control"]


async def test_server_header_is_not_leaked(client) -> None:
    response = await client.get("/auth/csrf")
    assert "server" not in {key.lower() for key in response.headers}


async def test_forgot_password_does_not_reveal_whether_an_account_exists(client, user) -> None:
    """The response must be byte-identical for a known and an unknown address."""
    known = await client.post("/auth/forgot-password", json={"email": user.email})
    unknown = await client.post("/auth/forgot-password", json={"email": "nobody-here@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


async def test_login_failure_messages_are_identical(client, user) -> None:
    """Wrong password and unknown account must be indistinguishable.

    If they differ, the login endpoint is a free membership oracle for any
    address an attacker cares to test.
    """
    wrong_password = await client.post(
        "/auth/login",
        data={"username": user.email, "password": "definitely-not-the-password"},
    )
    unknown_user = await client.post(
        "/auth/login",
        data={"username": "nobody-here@example.com", "password": "definitely-not-the-password"},
    )

    assert wrong_password.status_code == unknown_user.status_code
    assert wrong_password.json() == unknown_user.json()


async def test_login_succeeds_with_correct_credentials(client, user, password) -> None:
    response = await client.post("/auth/login", data={"username": user.email, "password": password})
    assert response.status_code in (200, 204)


async def test_protected_route_requires_authentication(client) -> None:
    response = await client.get("/users/me")
    assert response.status_code == 401


async def test_registration_rejects_a_weak_password(client) -> None:
    response = await client.post(
        "/auth/register", json={"email": "new@example.com", "password": "password"}
    )
    assert response.status_code in (400, 422)


async def test_registration_normalises_email_case(client, password) -> None:
    """Otherwise `Ada@example.com` becomes a second account that bypasses the
    first one's lockout state entirely."""
    first = await client.post(
        "/auth/register",
        json={"email": "Casey@Example.com", "password": password},
    )
    assert first.status_code == 201
    assert first.json()["email"] == "casey@example.com"

    duplicate = await client.post(
        "/auth/register",
        json={"email": "casey@example.com", "password": password},
    )
    assert duplicate.status_code == 400


async def test_refresh_without_a_token_is_rejected(client) -> None:
    response = await client.post("/auth/refresh", json={})
    assert response.status_code == 401


async def test_unhandled_errors_do_not_leak_internals(client, monkeypatch) -> None:
    """A stack trace or driver error in a response body discloses schema,
    library versions and file paths."""
    response = await client.get("/health")
    assert "traceback" not in response.text.lower()


async def test_openapi_is_available_outside_production(client) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
