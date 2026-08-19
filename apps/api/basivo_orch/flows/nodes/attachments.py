"""Reading the pictures people paste into issues.

A bug report is very often a screenshot: a stack trace photographed, a broken
layout, a red error toast. Handing the agent only the issue's text throws that
away, so this module turns the image references in an issue body into actual
bytes the model can look at.

Doing it safely is the whole job, because **an issue body is untrusted input**
— on a public repository anyone at all can write one. Two rules follow:

1. **The URL is not fetched unless its host is on the allowlist.** Otherwise
   "![](http://169.254.169.254/latest/meta-data/)" turns the fix bot into an
   SSRF probe against whatever network it runs in.
2. **The token is scoped per hop.** GitHub's attachment URLs redirect to signed
   object storage; following that redirect with the Authorization header still
   attached would hand a repo-write token to a third-party host. Auth is
   decided per hop from the host, so the redirect simply does not receive it.

Everything here is a pure function over (text) or (http, urls), so the rules
above are tested directly rather than inferred from a node's behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

#: Hosts whose images we will fetch at all. GitHub serves attachments from
#: `github.com/user-attachments/...` and every flavour of user content from a
#: `*.githubusercontent.com` subdomain (user-images, private-user-images,
#: objects, raw). Anything else is skipped and logged, not fetched.
_ALLOWED_HOST_SUFFIXES = (".githubusercontent.com",)
_ALLOWED_HOSTS = frozenset({"github.com", "www.github.com", "githubusercontent.com"})

#: Hosts the Authorization header may be sent to. Deliberately smaller than
#: the fetch allowlist: object storage serves a *signed* URL and needs no
#: credential, so sending one there is pure leak with no benefit.
_AUTH_HOSTS = frozenset({"github.com", "www.github.com", "api.github.com"})

#: What a model will actually accept as an image.
_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: Magic bytes, for servers that answer with a useless content-type.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: Ceilings. A model call carrying twenty full-page screenshots is a cost
#: incident; these bound it before the provider bills for it.
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3

#: `![alt](url)` and `<img src="url">`, the two ways an image reaches an issue
#: body — the first from a paste or drag-and-drop, the second from GitHub's own
#: editor when it records a width.
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(<?)(?P<url>[^)\s>]+)\1[^)]*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"'](?P<url>[^\"']+)[\"']", re.IGNORECASE)


#: `github.com/o/r/raw|blob/<ref>/<path>` and `raw.githubusercontent.com/o/r/<ref>/<path>`
#: — the ways an issue references a picture that lives *in the repository*.
_REPO_FILE = re.compile(
    r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/(?:raw|blob)/(?P<ref>[^/]+)/(?P<path>.+)$"
)
_RAW_HOST_FILE = re.compile(
    r"^https://raw\.githubusercontent\.com/(?P<repo>[^/]+/[^/]+)/(?P<ref>[^/]+)/(?P<path>.+)$"
)


def repo_file_reference(url: str) -> tuple[str, str, str] | None:
    """`(repo, ref, path)` if this URL names a file inside a GitHub repository.

    Worth recognising because those URLs cannot be fetched with an API token:
    `github.com/.../raw/...` is a *web* endpoint that answers a Bearer token
    with 404 and a login page, and raw.githubusercontent.com wants its own
    signed link. The same bytes come back cleanly from the Contents API, which
    is what the caller does with this. Found the hard way, against a real
    private repository.
    """
    for pattern in (_REPO_FILE, _RAW_HOST_FILE):
        if match := pattern.match(url.split("?")[0]):
            return match["repo"], match["ref"], match["path"]
    return None


@dataclass(frozen=True)
class FetchedImage:
    """One image, downloaded and ready to hand to a model."""

    url: str
    data: bytes
    media_type: str

    @property
    def kilobytes(self) -> int:
        return max(1, len(self.data) // 1024)


def extract_image_urls(text: str, *, limit: int = MAX_IMAGES) -> list[str]:
    """Image URLs referenced by an issue body, in the order they appear.

    Deduplicated — the same screenshot pasted twice is one image, and paying a
    vision model twice for it is the kind of waste nobody notices until the
    invoice.
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_MARKDOWN_IMAGE, _HTML_IMAGE):
        for match in pattern.finditer(text):
            url = match.group("url").strip()
            if url and url not in seen:
                seen.add(url)
                found.append(url)
    # Sort by where each first appears so the model sees them in reading
    # order, which the surrounding prose usually refers to ("as shown above").
    found.sort(key=text.find)
    return found[:limit]


def is_fetchable(url: str) -> bool:
    """Whether this URL may be fetched at all. https and allowlisted host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host in _ALLOWED_HOSTS or host.endswith(_ALLOWED_HOST_SUFFIXES)


def _may_authenticate(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() in _AUTH_HOSTS


def _sniff_media_type(response: httpx.Response, body: bytes) -> str | None:
    declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if declared in _IMAGE_MEDIA_TYPES:
        return declared
    for prefix, media_type in _MAGIC:
        if body.startswith(prefix):
            return media_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_media_type(body: bytes) -> str | None:
    """The media type of these bytes, from their magic number, or None."""
    for prefix, media_type in _MAGIC:
        if body.startswith(prefix):
            return media_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


async def fetch_image(
    http: httpx.AsyncClient, url: str, *, token: str | None
) -> FetchedImage | None:
    """Download one image, or return None with the reason logged by the caller.

    Redirects are followed by hand rather than with `follow_redirects=True`
    precisely so the Authorization header can be re-decided at every hop: httpx
    would carry the header we set straight through to the redirect target.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_fetchable(current):
            return None

        headers = {"Accept": "image/*"}
        if token and _may_authenticate(current):
            headers["Authorization"] = f"Bearer {token}"

        response = await http.get(current, headers=headers, follow_redirects=False)

        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return None
            current = str(httpx.URL(current).join(location))
            continue

        if response.status_code != 200:
            return None

        body = response.content
        if not body or len(body) > MAX_IMAGE_BYTES:
            return None
        media_type = _sniff_media_type(response, body)
        if media_type is None:
            return None
        return FetchedImage(url=url, data=body, media_type=media_type)

    return None
