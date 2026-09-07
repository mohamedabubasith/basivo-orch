"""Rendering video with Remotion.

A composition is a React component. The author writes one file, and this
module puts it inside a project that already has the dependencies installed,
bundles it once, looks at a few frames, and encodes.

Three decisions worth stating, because each replaced something that used to
go wrong:

**The product owns narration, captions and duration.** They are siblings of
the author's component in `Root.tsx`, not markup spliced into what the author
wrote. The previous renderer edited the composition's HTML as a string, and
every failure of that kind was silent: a stray tag swallowed everything after
it, and a composition that added its own captions rendered two sets on top of
each other.

**Frames are checked before the encode, from the same bundle.** A composition
that compiles and renders four seconds of empty gradient is the worst
outcome, because nothing failed. Stills cost about a second each; a wasted
render costs minutes.

**node_modules is baked into the image, never installed per render.** A render
that runs `npm install` first is a render that fails the day the registry is
slow, and it would run inside a subprocess that deliberately has no
credentials and no reason to reach the network at all.

Licence note: Remotion is source-available, not open source. It is free for
individuals and organisations of up to three people. A larger company running
this needs a Remotion company licence, and so does anyone self-hosting this
product at that size. See docs/video.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from basivo_orch.flows.nodes.base import NodeError

#: The project shipped with this package: package.json, the entry point, the
#: root that wraps a composition, and the render script.
PROJECT_ROOT = Path(__file__).with_name("remotion_project")

#: Where the installed dependencies live. The worker image installs them once
#: into the project itself; a developer machine can point elsewhere.
NODE_MODULES_ENV = "BASIVO_REMOTION_NODE_MODULES"

#: How long one render may take. Generous, because a minute of 1080p is real
#: work, but finite: a browser that never finishes a frame otherwise holds a
#: worker slot forever.
RENDER_TIMEOUT_SECONDS = 900

#: A separate, much shorter limit for the frame check. It bundles and takes a
#: handful of small stills; if that has not finished, the composition is doing
#: something no encode is going to survive either.
PROBE_TIMEOUT_SECONDS = 240

#: The longest video this will render. Past this the wait and the file size
#: stop being reasonable for a queue that also runs everything else, and the
#: right answer is to render it in parts.
MAX_DURATION_SECONDS = 120

#: A render will not start with less than this much free disk. Frames for a
#: 30-second 1080p video are hundreds of megabytes before they are encoded and
#: thrown away, and on a box where the database shares the volume, filling the
#: disk does not fail a render, it stops Postgres accepting writes.
MIN_FREE_DISK_GB = 2.0

CODECS: dict[str, str] = {"mp4": "h264", "webm": "vp8", "gif": "gif"}

#: Quality, expressed the way the encoder wants it. Lower CRF is better and
#: bigger; these three were picked by looking at the output, not by theory.
CRF: dict[str, int] = {"draft": 30, "standard": 23, "high": 18}


@dataclass(frozen=True, slots=True)
class RenderJob:
    """Everything one render needs, in the shape the script expects."""

    scene_tsx: str
    width: int
    height: int
    fps: int
    duration_seconds: float
    props: dict[str, Any]
    background: str = "#0b1020"
    fmt: str = "mp4"
    quality: str = "standard"
    #: Files the composition may refer to by name through `staticFile`.
    assets: dict[str, bytes] | None = None
    #: Narration, already measured. The filename must also be in `assets`.
    audio: str = ""
    captions: list[dict[str, Any]] | None = None
    concurrency: int | None = None

    @property
    def duration_in_frames(self) -> int:
        # At least one frame: a composition of zero frames is not a video, and
        # Remotion refuses it with an error about the composition rather than
        # about the duration somebody typed.
        return max(1, round(self.duration_seconds * self.fps))


def node_modules_path() -> Path:
    override = os.environ.get(NODE_MODULES_ENV, "").strip()
    return Path(override) if override else PROJECT_ROOT / "node_modules"


def is_installed() -> bool:
    return (node_modules_path() / "remotion").is_dir()


def free_disk_gb(path: str | None = None) -> float:
    usage = shutil.disk_usage(path or tempfile.gettempdir())
    return usage.free / 1_000_000_000


def ensure_disk_space(minimum_gb: float = MIN_FREE_DISK_GB) -> None:
    free = free_disk_gb()
    if free < minimum_gb:
        raise NodeError(
            f"Only {free:.1f} GB of disk is free and a render needs about {minimum_gb:.0f} GB. "
            "Free some space and try again."
        )


def _prepare(work: Path, job: RenderJob) -> Path:
    """Lay out one render's project. Returns the project root inside `work`.

    The shipped project is copied rather than rendered in place, because two
    runs render at the same time and each writes its own composition into
    `src/Scene.tsx`. `node_modules` is linked, not copied: it is hundreds of
    megabytes and identical every time.
    """
    project = work / "project"
    shutil.copytree(PROJECT_ROOT, project, ignore=shutil.ignore_patterns("node_modules"))

    modules = node_modules_path()
    if not (modules / "remotion").is_dir():
        raise NodeError(
            "The video renderer is not installed on this server. Install it by running "
            "`npm install` inside basivo_orch/flows/nodes/remotion_project, or point "
            f"{NODE_MODULES_ENV} at an installed copy."
        )
    (project / "node_modules").symlink_to(modules, target_is_directory=True)

    (project / "src" / "Scene.tsx").write_text(job.scene_tsx, encoding="utf-8")

    public = project / "public"
    public.mkdir(exist_ok=True)
    for name, blob in (job.assets or {}).items():
        # Only the file name: a composition asking for ../../etc/passwd is a
        # composition written by a model, and this is where that stops.
        (public / Path(name).name).write_bytes(blob)

    (project / "src" / "job.json").write_text(
        json.dumps(
            {
                "width": job.width,
                "height": job.height,
                "fps": job.fps,
                "durationInFrames": job.duration_in_frames,
                "background": job.background,
                "props": job.props,
                "captions": job.captions or [],
                "audio": Path(job.audio).name if job.audio else "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return project


def _environment(work: Path) -> dict[str, str]:
    """A stripped environment.

    The renderer runs somebody else's JavaScript. It has no business seeing
    SECRET_KEY, DATABASE_URL, or a model key, and a composition that tried to
    read one would find nothing to read.
    """
    keep = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(work)),
        "TMPDIR": str(work),
        "CI": "1",
        "NO_COLOR": "1",
    }
    # Where the browser lives, when the image put it somewhere specific. Named
    # explicitly rather than inherited wholesale.
    for passthrough in (
        "REMOTION_CHROME_EXECUTABLE",
        "REMOTION_BROWSER_EXECUTABLE",
        "PUPPETEER_EXECUTABLE_PATH",
        "XDG_CACHE_HOME",
    ):
        if value := os.environ.get(passthrough):
            keep[passthrough] = value
    return keep


async def _run_script(
    project: Path, work: Path, spec: dict[str, Any], limit_seconds: float
) -> dict:
    """Run render.mjs and read its one JSON line. Raises NodeError on failure."""
    job_file = work / "job-spec.json"
    job_file.write_text(json.dumps(spec), encoding="utf-8")

    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(project / "render.mjs"),
            str(job_file),
            cwd=str(project),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_environment(work),
        )
    except FileNotFoundError as exc:
        raise NodeError(
            "Node is not installed on this server, and the video renderer needs it. "
            "Node 20 or newer."
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=limit_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise NodeError(
            f"The render did not finish within {int(limit_seconds)} seconds. Shorten the "
            "video, lower the quality, or reduce the frame rate."
        ) from None

    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")

    for line in reversed(out.splitlines()):
        if line.startswith("RESULT "):
            payload = json.loads(line[len("RESULT ") :])
            if payload.get("ok"):
                return payload
            raise NodeError(_readable(str(payload.get("error", "")).strip() or err))

    raise NodeError(_readable(err or out or "The renderer said nothing at all."))


#: Errors a person can act on, from errors only a bundler understands. Each
#: pattern here is a failure that was seen and misread at least once.
_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    (
        "Module not found",
        "The composition imports a package that is not available. Only `react` and "
        "`remotion` can be imported.",
    ),
    (
        "Unexpected token",
        "The composition does not compile. There is a syntax error in it.",
    ),
    (
        "Cannot find module",
        "The composition imports a file that does not exist.",
    ),
    (
        "ENOSPC",
        "The server ran out of disk while rendering.",
    ),
    (
        "Could not find the browser",
        "The browser the renderer needs is not installed on this server. Run "
        "`npx remotion browser ensure` in the renderer project.",
    ),
)


def _readable(raw: str) -> str:
    text = raw.strip()
    for needle, sentence in _TRANSLATIONS:
        if needle in text:
            return f"{sentence}\n\nThe renderer said:\n{text[-1200:]}"
    return f"The render failed.\n\nThe renderer said:\n{text[-1500:]}"


def _read_probes(result: dict[str, Any]) -> list[bytes]:
    """The still images, as bytes. Sync on purpose: reading four small files
    is not worth a thread, and the lint that forbids pathlib in async code is
    right about the general case."""
    frames: list[bytes] = []
    for item in result.get("probes", []):
        path = Path(item["path"])
        if path.exists():
            frames.append(path.read_bytes())
    return frames


async def probe(job: RenderJob, *, frames: list[int], scale: float = 0.25) -> list[bytes]:
    """Render a few stills and return them as PNG bytes, encoding nothing.

    Kept separate from `render` so a composition can be rejected and rewritten
    before the expensive part starts. The bytes are returned rather than
    paths: the caller only ever looks at pixels, and a path would outlive the
    directory it points into.
    """
    ensure_disk_space()
    with tempfile.TemporaryDirectory(prefix="basivo-remotion-probe-") as workdir:
        work = Path(workdir)
        project = _prepare(work, job)
        result = await _run_script(
            project,
            work,
            {
                "projectRoot": str(project),
                "workDir": str(work),
                "probeFrames": frames,
                "probeScale": scale,
                "probeOnly": True,
            },
            PROBE_TIMEOUT_SECONDS,
        )
        return _read_probes(result)


async def render(job: RenderJob) -> tuple[bytes, dict[str, Any]]:
    """Encode the video. Returns the bytes and what the renderer reported."""
    ensure_disk_space()
    with tempfile.TemporaryDirectory(prefix="basivo-remotion-") as workdir:
        work = Path(workdir)
        project = _prepare(work, job)
        output = work / f"out.{job.fmt}"

        result = await _run_script(
            project,
            work,
            {
                "projectRoot": str(project),
                "workDir": str(work),
                "output": str(output),
                "codec": CODECS.get(job.fmt, "h264"),
                "crf": CRF.get(job.quality, 23),
                "concurrency": job.concurrency,
                "probeFrames": [],
            },
            RENDER_TIMEOUT_SECONDS,
        )

        if not output.exists():
            raise NodeError("The renderer finished but wrote no file.")
        return output.read_bytes(), result
