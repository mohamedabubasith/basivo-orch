"""The images a person uploads for their app to use.

A page about a bakery needs a photograph of the bakery, and a model cannot
draw one. So an upload lands here, the turn writes it into `public/uploads/`
before the agent starts, and the agent refers to it as `/uploads/name.png`
exactly as it would any other file in `public/`.

Three rules, and each of them is here rather than in the route because the
route is not the only caller that will ever exist:

**The bytes decide the type, not the name and not the browser.** A file called
`logo.png` that is really an HTML document would be served from the app's own
address, and `Content-Type` sniffing is how that becomes somebody else's
script. The first bytes are read and anything unrecognised is refused.

**The name is ours.** Whatever was typed is reduced to letters, digits and
dashes, so a name cannot contain a slash, a `..`, or a leading dot, and cannot
collide with a file the template owns.

**There is a ceiling per file and per project.** Storage is the one resource a
deployment cannot overcommit: the workspace-wide limit is the plan's, enforced
in `billing.service`, and these two keep one project from spending all of it.
"""

from __future__ import annotations

import re

#: One image. Large enough for a photograph a phone took, small enough that a
#: page carrying twenty of them still loads on a train.
MAX_ASSET_BYTES = 2 * 1024 * 1024

#: Per project, so one app cannot take a workspace's whole allowance.
MAX_PROJECT_ASSETS = 20
MAX_PROJECT_ASSET_BYTES = 12 * 1024 * 1024

#: Where they appear in the tree, and therefore the URL: `public/uploads/a.png`
#: is served at `/uploads/a.png`.
UPLOAD_DIR = "public/uploads/"


class AssetRefused(ValueError):
    """The file cannot be stored, with the sentence the person reads."""


def sniff(data: bytes) -> tuple[str, str]:
    """The content type and extension these bytes actually are.

    Raises `AssetRefused` for anything that is not an image. A page may only
    hold what a browser will paint: everything else served from an app's own
    address is a way to host something that is not an app.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    head = data[:400].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2000].lower()):
        # Served inside the app's own sandboxed, opaque origin, so a script
        # inside one can reach nothing: not the console, not a cookie, not
        # another app. Refusing SVG would refuse most logos.
        return "image/svg+xml", ".svg"
    raise AssetRefused(
        "That file is not an image. Upload a PNG, JPEG, GIF, WebP or SVG, or paste the "
        "text you want into the message instead."
    )


def safe_name(name: str, extension: str) -> str:
    """A file name of our own making, ending in the extension the bytes proved."""
    stem = re.sub(r"[^a-z0-9]+", "-", name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()).strip("-")
    return f"{stem[:48] or 'image'}{extension}"


def unique_name(wanted: str, taken: set[str]) -> str:
    """`logo.png`, `logo-2.png`, `logo-3.png`. Nothing is ever overwritten.

    Two uploads of the same name are two different pictures far more often
    than they are a correction, and a page that silently changed because
    somebody uploaded a second logo is a bug nobody can see.
    """
    if wanted not in taken:
        return wanted
    stem, _, extension = wanted.rpartition(".")
    for suffix in range(2, 100):
        candidate = f"{stem}-{suffix}.{extension}"
        if candidate not in taken:
            return candidate
    raise AssetRefused("Too many images with that name. Rename the file and upload it again.")


def check_room(data: bytes, *, count: int, used: int) -> None:
    """Refuse a file this project has no room for, saying which limit it hit."""
    if not data:
        raise AssetRefused("That file is empty.")
    if len(data) > MAX_ASSET_BYTES:
        raise AssetRefused(
            f"That image is {len(data) // (1024 * 1024) or 1} MB and the limit is "
            f"{MAX_ASSET_BYTES // (1024 * 1024)} MB. Save it smaller and upload it again."
        )
    if count >= MAX_PROJECT_ASSETS:
        raise AssetRefused(
            f"This app already has {MAX_PROJECT_ASSETS} images. Delete one you are not "
            "using and upload this again."
        )
    if used + len(data) > MAX_PROJECT_ASSET_BYTES:
        raise AssetRefused(
            f"This app's images would go over {MAX_PROJECT_ASSET_BYTES // (1024 * 1024)} MB. "
            "Delete one you are not using, or upload a smaller file."
        )
