"""Reading images out of an issue body — and refusing to read the wrong ones.

An issue body is written by whoever opened the issue, which on a public
repository is anyone at all. Two of these tests are the security of the
feature rather than its behaviour:

* `test_the_token_is_never_sent_to_the_redirect_target` — GitHub attachment
  URLs redirect to signed object storage. Carrying the Authorization header
  through that hop would hand a repo-write token to a third party.
* the allowlist tests — an image URL is a URL the *reporter* chose, so
  fetching it unconditionally makes the fix bot an SSRF probe.
"""

from __future__ import annotations

import httpx
import pytest

from basivo_orch.flows.nodes.attachments import (
    MAX_IMAGE_BYTES,
    extract_image_urls,
    fetch_image,
    is_fetchable,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
ATTACHMENT = "https://github.com/user-attachments/assets/2f1c-4b7e"
SIGNED = "https://private-user-images.githubusercontent.com/9/img.png?jwt=abc"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_markdown_and_html_images_are_both_found_in_reading_order():
    body = f"""The login page breaks:

![screenshot]({ATTACHMENT})

and the console shows:

<img width="600" alt="console" src="https://github.com/user-attachments/assets/9999">
"""
    assert extract_image_urls(body) == [
        ATTACHMENT,
        "https://github.com/user-attachments/assets/9999",
    ]


def test_the_same_image_pasted_twice_is_one_image():
    body = f"![a]({ATTACHMENT})\n\nand again:\n\n![a]({ATTACHMENT})"
    assert extract_image_urls(body) == [ATTACHMENT]


def test_a_body_with_no_images_yields_nothing():
    assert extract_image_urls("Plain text bug report, no pictures.") == []
    assert extract_image_urls("") == []


def test_links_that_are_not_images_are_ignored():
    # A normal markdown link is not an image reference; only `![...]` is.
    assert extract_image_urls(f"See [the docs]({ATTACHMENT}) for details.") == []


def test_the_image_count_is_capped():
    body = "\n".join(f"![s{i}](https://github.com/user-attachments/assets/{i})" for i in range(20))
    assert len(extract_image_urls(body)) == 6


# ---------------------------------------------------------------------------
# The allowlist — an SSRF gate, not a formatting check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        ATTACHMENT,
        SIGNED,
        "https://user-images.githubusercontent.com/1/x.png",
        "https://objects.githubusercontent.com/foo",
    ],
)
def test_github_hosts_are_fetchable(url):
    assert is_fetchable(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1:8000/admin",  # loopback
        "https://internal.corp/secrets.png",  # anything private
        "https://evil.example.com/x.png",  # any third party
        "http://github.com/user-attachments/assets/1",  # plain http
        "file:///etc/passwd",
        "https://github.com.evil.example.com/x.png",  # suffix-confusion
        "https://notgithubusercontent.com/x.png",
    ],
)
def test_everything_else_is_refused(url):
    assert not is_fetchable(url)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def test_an_authenticated_attachment_is_downloaded():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        image = await fetch_image(http, ATTACHMENT, token="ghp_secret")

    assert image is not None
    assert image.data == PNG
    assert image.media_type == "image/png"
    assert seen[0].headers["Authorization"] == "Bearer ghp_secret"


async def test_the_token_is_never_sent_to_the_redirect_target():
    """The leak this whole design exists to prevent.

    `follow_redirects=True` would carry our Authorization header to whatever
    host GitHub redirects to. The token is decided per hop instead, so signed
    object storage receives the request with no credential attached.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": SIGNED})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        image = await fetch_image(http, ATTACHMENT, token="ghp_secret")

    assert image is not None, "the redirect was not followed at all"
    assert len(seen) == 2
    assert seen[0].headers["Authorization"] == "Bearer ghp_secret"
    assert "authorization" not in {k.lower() for k in seen[1].headers}, (
        "the repo-write token was sent to the object-storage host"
    )


async def test_a_redirect_off_the_allowlist_is_not_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.png"})
        raise AssertionError("followed a redirect to a host that is not allowed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch_image(http, ATTACHMENT, token="t") is None


async def test_a_response_that_is_not_an_image_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>login page</html>", headers={"content-type": "text/html"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch_image(http, ATTACHMENT, token="t") is None


async def test_content_type_is_sniffed_when_the_server_lies():
    """Object storage often answers `application/octet-stream`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=PNG, headers={"content-type": "application/octet-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        image = await fetch_image(http, ATTACHMENT, token="t")

    assert image is not None and image.media_type == "image/png"


async def test_an_oversized_image_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        huge = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1)
        return httpx.Response(200, content=huge, headers={"content-type": "image/png"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch_image(http, ATTACHMENT, token="t") is None


async def test_a_missing_attachment_is_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch_image(http, ATTACHMENT, token="t") is None


async def test_a_redirect_loop_terminates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": ATTACHMENT})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await fetch_image(http, ATTACHMENT, token="t") is None


# ---------------------------------------------------------------------------
# Repo-hosted pictures — found the hard way against a real private repository
# ---------------------------------------------------------------------------


def test_urls_that_name_a_file_in_a_repository_are_recognised():
    """Those cannot be fetched with an API token: github.com/.../raw/ is a web
    endpoint that answers a Bearer token with 404 and a login page. They have
    to go through the Contents API instead, so they must be spotted first."""
    from basivo_orch.flows.nodes.attachments import repo_file_reference

    assert repo_file_reference("https://github.com/acme/api/raw/main/docs/bug.png") == (
        "acme/api",
        "main",
        "docs/bug.png",
    )
    assert repo_file_reference("https://github.com/acme/api/blob/main/docs/bug.png?raw=true") == (
        "acme/api",
        "main",
        "docs/bug.png",
    )
    assert repo_file_reference("https://raw.githubusercontent.com/acme/api/main/b.png") == (
        "acme/api",
        "main",
        "b.png",
    )
    # A pasted attachment is NOT a repo file — it takes the plain fetch path.
    assert repo_file_reference(ATTACHMENT) is None
    assert repo_file_reference("https://example.com/x.png") is None


def test_image_media_type_reads_magic_numbers():
    from basivo_orch.flows.nodes.attachments import image_media_type

    assert image_media_type(PNG) == "image/png"
    assert image_media_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert image_media_type(b"GIF89a...") == "image/gif"
    assert image_media_type(b"<html>login</html>") is None
