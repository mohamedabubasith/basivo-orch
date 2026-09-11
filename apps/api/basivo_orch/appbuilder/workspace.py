"""Where an app is edited and built, and what survives between messages.

The person types "make the header smaller" and expects the agent to know what
header. Nothing about that requires a machine kept running for them: what has
to survive is the **files**, and a coding agent walks into a directory it has
never seen and works out what is there. So a project stores its tree, and a
turn is: unpack the tree, let an agent edit it, build it, pack it up again.

That is why this module is a protocol with one small implementation rather
than a scheduler. `TempWorkspace` is a directory on a stateless worker.
`ContainerWorkspace` becomes worth writing the first time somebody needs what
a directory cannot give: installing a package the template does not carry,
running tests, a dev server with hot reload, a backend. Until then a container
per person would buy two seconds of build time and cost a scheduler, an idle
reaper, an authenticating proxy and per person memory.

Three things here are load bearing:

**`node_modules` is never installed at turn time.** It is baked into the
worker image and linked in, exactly as the video renderer's is. No network
during a build, no supply chain surface, and a build measured in seconds.

**Writes are confined to `src/`, `public/` and `index.html`.** The agent has
no shell, so the guard is a diff taken afterwards: anything outside those is
refused and the turn fails rather than being quietly dropped, because a half
applied change is worse than none.

**`AGENTS.md` is written by us every turn.** The agent must not be able to
edit the rules it is working under, and a template whose rules have improved
should improve every existing project the next time it is touched.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from basivo_orch.flows.nodes.base import NodeError
from basivo_orch.logging import get_logger

log = get_logger(__name__)

#: The project every app starts from.
TEMPLATE_ROOT = Path(__file__).with_name("template")

#: Where the installed dependencies live. The worker image installs them once
#: into the template itself; a developer machine can point elsewhere.
NODE_MODULES_ENV = "BASIVO_APP_NODE_MODULES"

#: What an agent may write. Everything else in the project is ours: the build
#: configuration, the dependency list, and the rules themselves.
WRITABLE: tuple[str, ...] = ("src/", "public/", "index.html")

#: Never stored with the source: two are generated, one is a symlink to a
#: directory of hundreds of megabytes.
NOT_SOURCE: tuple[str, ...] = ("node_modules/", "dist/", "AGENTS.md", ".git/")

#: A frontend that does not fit in this is not a frontend, it is an asset
#: dump, and both halves have to fit in an artifact row.
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_DIST_BYTES = 24 * 1024 * 1024

#: Long enough for a cold Vite build of a page with a hundred components,
#: short enough that a build which is not going to finish stops holding a
#: worker slot.
BUILD_TIMEOUT_SECONDS = 240.0


@dataclass
class BuildResult:
    """What a build produced, or what it said when it could not."""

    ok: bool
    log: str
    #: The `dist` directory as one gzipped tar, empty when the build failed.
    dist: bytes = b""
    seconds: float = 0.0


class Workspace(Protocol):
    """One project's files, for the length of one turn."""

    async def open(self, source: bytes | None) -> Path:
        """Lay out the tree and return its root."""
        ...

    async def build(self, root: Path) -> BuildResult:
        """Produce the files a browser will load."""
        ...

    async def close(self, root: Path) -> bytes:
        """The tree, packed, to store against the project."""
        ...

    async def discard(self, root: Path) -> None:
        """Throw the working copy away, keeping nothing."""
        ...


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


def pack(root: Path, skip: tuple[str, ...] = NOT_SOURCE, limit: int = MAX_SOURCE_BYTES) -> bytes:
    """A directory as one gzipped tar, with the generated parts left out."""
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if any(relative == entry.rstrip("/") or relative.startswith(entry) for entry in skip):
                continue
            total += path.stat().st_size
            if total > limit:
                raise NodeError(
                    f"The project is larger than {limit // (1024 * 1024)}MB. Remove the large "
                    "files from it; this builder is for pages, not for asset storage."
                )
            tar.add(path, arcname=relative)
    return buffer.getvalue()


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file the agent could have touched, with its content.

    The diff taken from two of these is the truth about a turn. An agent's own
    account of what it edited is a claim, and the guard on protected paths has
    to run against facts.
    """
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == entry.rstrip("/") or relative.startswith(entry) for entry in NOT_SOURCE):
            continue
        files[relative] = path.read_bytes()
    return files


def unpack(archive: bytes, into: Path) -> None:
    """Restore a packed tree. Refuses anything that would write outside."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            # `filter="data"` is what makes an absolute path, a `..` or a
            # symlink out of the tree refuse to extract rather than land.
            tar.extractall(into, filter="data")
    except tarfile.TarError as exc:
        raise NodeError(f"The saved project could not be restored: {exc}.") from None


# ---------------------------------------------------------------------------
# The beta implementation
# ---------------------------------------------------------------------------


def node_modules_path() -> Path:
    override = os.environ.get(NODE_MODULES_ENV, "").strip()
    return Path(override) if override else TEMPLATE_ROOT / "node_modules"


def is_installed() -> bool:
    return (node_modules_path() / "vite").is_dir()


@dataclass
class TempWorkspace:
    """A directory on the worker, for one turn, deleted afterwards."""

    #: Kept so the caller can delete the directory it was given a path into.
    _roots: dict[Path, tempfile.TemporaryDirectory] = field(default_factory=dict)

    async def open(self, source: bytes | None) -> Path:
        return await asyncio.to_thread(self._open, source)

    def _open(self, source: bytes | None) -> Path:
        holder = tempfile.TemporaryDirectory(prefix="basivo-app-")
        root = Path(holder.name) / "app"
        if source:
            root.mkdir(parents=True)
            unpack(source, root)
        else:
            shutil.copytree(
                TEMPLATE_ROOT, root, ignore=shutil.ignore_patterns("node_modules", "dist")
            )

        modules = node_modules_path()
        if not (modules / "vite").is_dir():
            raise NodeError(
                "The app builder's dependencies are not installed on this server. Run "
                "`npm install` inside basivo_orch/appbuilder/template, or point "
                f"{NODE_MODULES_ENV} at an installed copy."
            )
        (root / "node_modules").symlink_to(modules, target_is_directory=True)

        # Ours, every turn: an agent must not be able to edit the rules it
        # works under, and a project built last month should pick up the rules
        # as they are today.
        shutil.copyfile(TEMPLATE_ROOT / "AGENTS.md", root / "AGENTS.md")
        # A project saved before a configuration change gets the new one, for
        # the same reason. These are not the agent's files.
        for name in ("package.json", "vite.config.ts", "tsconfig.json"):
            shutil.copyfile(TEMPLATE_ROOT / name, root / name)

        self._roots[root] = holder
        return root

    async def build(self, root: Path) -> BuildResult:
        started = asyncio.get_running_loop().time()
        vite = node_modules_path() / "vite" / "bin" / "vite.js"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(vite),
            "build",
            cwd=str(root),
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(root),
                "NODE_ENV": "production",
                "CI": "1",
                "NO_COLOR": "1",
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=BUILD_TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            return BuildResult(
                ok=False,
                log=f"The build did not finish within {BUILD_TIMEOUT_SECONDS:.0f} seconds.",
                seconds=asyncio.get_running_loop().time() - started,
            )

        text = out.decode(errors="replace")
        seconds = asyncio.get_running_loop().time() - started
        if process.returncode != 0 or not (root / "dist" / "index.html").exists():
            return BuildResult(ok=False, log=_tail(text), seconds=seconds)
        dist = await asyncio.to_thread(pack, root / "dist", (), MAX_DIST_BYTES)
        return BuildResult(ok=True, log=_tail(text), dist=dist, seconds=seconds)

    async def close(self, root: Path) -> bytes:
        source = await asyncio.to_thread(pack, root)
        await self.discard(root)
        return source

    async def discard(self, root: Path) -> None:
        """Delete the directory without keeping what is in it.

        Every path out of a turn ends here, including the failed ones: a
        worker that leaks a working copy per failure fills its disk in a day.
        """
        holder = self._roots.pop(root, None)
        if holder is not None:
            await asyncio.to_thread(holder.cleanup)


def _tail(text: str, limit: int = 4000) -> str:
    """The end of a build log, which is where the error is."""
    text = text.strip()
    return text if len(text) <= limit else "[earlier output trimmed]\n" + text[-limit:]


def refused_writes(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    """Paths the agent changed that it was not allowed to change."""
    touched = {path for path, blob in after.items() if before.get(path) != blob}
    touched |= {path for path in before if path not in after}
    return sorted(
        path
        for path in touched
        if not any(path == entry or path.startswith(entry) for entry in WRITABLE)
    )
