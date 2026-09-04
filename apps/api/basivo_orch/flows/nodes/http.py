"""The generic outbound HTTP node, and the guard that keeps it from being an
attack tool pointed at our own network.

A node that fetches a URL chosen by whoever wrote the flow is server-side
request forgery by construction. Without a guard it will happily fetch
`http://169.254.169.254/latest/meta-data/iam/security-credentials/` and hand
the instance's cloud credentials back as node output — into a run log, and on
into the next node. This is the single most-exploited class of bug in workflow
tools, so the check lives here rather than being left to the caller.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import DEFAULT_PORT, Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3


class BlockedRequest(NodeError):
    """The target is not somewhere flows are allowed to reach."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


def assert_public_url(url: str) -> None:
    """Refuse anything that is not a public HTTP(S) endpoint.

    Every address the hostname resolves to is checked, not just the first: an
    attacker controlling DNS can return one public and one private answer and
    rely on the client picking the private one.

    Residual risk, stated rather than hidden: this resolves and then lets httpx
    resolve again when it connects, so a name whose answer changes between the
    two (DNS rebinding) can still slip through. Closing that needs the
    connection pinned to the validated address, which is a transport-level
    change. Deployments that care should also block egress to link-local and
    RFC1918 ranges at the network, which is the durable fix.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise BlockedRequest(f"Only http and https are allowed; {parsed.scheme or 'that'} is not.")
    if not parsed.hostname:
        raise BlockedRequest("That URL has no host.")

    host = parsed.hostname

    # A literal address skips DNS entirely.
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise NodeError(f"Could not resolve {host!r}.", retryable=True) from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]

    if not addresses:
        raise BlockedRequest(f"{host!r} did not resolve to any address.")

    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            # Deliberately does not say which address it resolved to: that
            # answer is itself a small internal-network scanner.
            raise BlockedRequest(
                f"{host!r} resolves to an internal address. Flows may only reach public endpoints."
            )


class HttpRequestConfig(BaseModel):
    url: str = Field(min_length=1, description="Supports {{ references }}.")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    body_type: Literal["json", "form", "text", "none"] = "json"
    timeout_seconds: float = Field(default=30.0, ge=1, le=120)
    #: When false, a 4xx/5xx becomes the node's output instead of failing the
    #: run — so a flow can branch on the status rather than blow up.
    fail_on_error_status: bool = True


async def _read_capped(response: httpx.Response, method: str, url: str) -> bytes:
    """Read a response body, refusing to exceed `MAX_RESPONSE_BYTES`.

    Two guards, because they catch different lies. A declared `content-length`
    over the limit is refused before a single byte of body is transferred. A
    server that declares nothing — or under-declares — is caught by counting
    what actually arrives and closing the connection the moment it goes over.
    """
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        await response.aclose()
        raise NodeError(
            f"{method} {url} declares {int(declared)} bytes and the limit is "
            f"{MAX_RESPONSE_BYTES}. Nothing was downloaded."
        )

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise NodeError(
                    f"{method} {url} sent more than {MAX_RESPONSE_BYTES} bytes; the transfer "
                    "was stopped. Fetch large files with a URL a later node can hand on, "
                    "rather than through this node."
                )
            chunks.append(chunk)
    finally:
        await response.aclose()
    return b"".join(chunks)


class HttpRequestNode(Node):
    type = "http.request"
    label = "HTTP Request"
    description = "Call any HTTP endpoint."
    when = (
        "You need data from, or want to send data to, a service no other node covers. Headers "
        "and body take templates from earlier nodes."
    )
    needs = (
        "A trigger before it, or any node whose output it should work on",
        "The endpoint's URL and any token it wants, ideally from a credential.",
    )
    example = "Schedule -> HTTP Request -> Set Variables -> Post to Social"
    tier = 1
    category = "utility"
    config_model = HttpRequestConfig
    output_paths = ("status", "headers", "body")

    max_attempts = 3
    replay_safe = False
    retry_backoff_seconds = 1.0
    timeout_seconds = 125.0

    async def run(self, config: HttpRequestConfig, ctx: NodeContext) -> NodeResult:
        context = ctx.template_context()
        url = str(render_value(config.url, context))
        assert_public_url(url)

        headers = {str(k): str(v) for k, v in render_value(config.headers, context).items()}
        params = render_value(config.query, context)
        body = render_value(config.body, context)

        kwargs: dict[str, Any] = {}
        if config.body_type == "json" and body is not None:
            kwargs["json"] = body
        elif config.body_type == "form" and body is not None:
            kwargs["data"] = body
        elif config.body_type == "text" and body is not None:
            kwargs["content"] = str(body)

        await ctx.progress(f"{config.method} {url}")

        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                # Streamed, not fetched whole. The size limit below used to run
                # after `response.content`, which meant a 2GB reply was already
                # downloaded and already in memory before being rejected — a
                # bill for the bandwidth and an OOM-killed worker, in exchange
                # for an error message. Now the transfer is stopped instead.
                request = ctx.http.build_request(
                    config.method,
                    current,
                    headers=headers,
                    params=params or None,
                    timeout=config.timeout_seconds,
                    **kwargs,
                )
                response = await ctx.http.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException as exc:
                raise NodeError(
                    f"{config.method} {current} timed out after {config.timeout_seconds}s.",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise NodeError(f"{config.method} {current} failed: {exc}", retryable=True) from exc

            # Redirects are followed by hand so every hop is re-validated. With
            # httpx's own following, a public URL that 302s to 169.254.169.254
            # would sail straight past the check above.
            if response.is_redirect and (location := response.headers.get("location")):
                await response.aclose()
                if hop == MAX_REDIRECTS:
                    raise NodeError(f"Too many redirects from {url}.")
                current = str(httpx.URL(current).join(location))
                assert_public_url(current)
                await ctx.progress(f"redirected to {current}")
                continue
            break

        raw = await _read_capped(response, config.method, url)

        content_type = response.headers.get("content-type", "")
        # Decoded from the bytes we captured, not from the response: a streamed
        # body is consumed and closed, so `.json()` and `.text` would raise
        # rather than return what we already hold.
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        parsed_body: Any
        if "json" in content_type:
            try:
                parsed_body = json.loads(text)
            except ValueError:
                parsed_body = text
        else:
            parsed_body = text

        if config.fail_on_error_status and response.status_code >= 400:
            raise NodeError(
                f"{config.method} {url} returned {response.status_code}.",
                # 4xx means the request was wrong and will stay wrong; 5xx and
                # 429 are worth another go.
                retryable=response.status_code >= 500 or response.status_code == 429,
            )

        return NodeResult(
            output={
                "status": response.status_code,
                "headers": dict(response.headers),
                "body": parsed_body,
            }
        )


__all__ = [
    "HttpRequestNode",
    "HttpRequestConfig",
    "assert_public_url",
    "BlockedRequest",
    "DEFAULT_PORT",
]
