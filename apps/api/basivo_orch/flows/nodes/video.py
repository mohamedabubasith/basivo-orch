"""Rendering video from HTML, through HyperFrames.

Same argument as the poster node, one step further: a model writes HTML and a
browser renders it, frame by frame, into an MP4. HeyGen's HyperFrames does the
hard half — seeking a GSAP timeline deterministically, capturing every frame in
headless Chrome, and handing it to FFmpeg — under Apache 2.0. Writing that
ourselves would be months of work to arrive somewhere worse.

Two consequences worth stating plainly.

**This executes JavaScript.** The poster node renders with JS disabled; a video
cannot, because the animation *is* JavaScript. A composition is therefore code,
at the same trust level as the Python code node — treat one from an untrusted
source the way you would treat a script from an untrusted source. The
subprocess gets a stripped environment (no credentials, no database URL) and a
hard wall-clock limit; running with `--docker` is the isolation upgrade, and
the composition never touches the API process.

**Video is slow and large.** Seconds of footage take tens of seconds to render
and megabytes to store, so duration and resolution are capped here rather than
discovered when a worker runs out of memory or the artifact ceiling rejects the
result after eight minutes of work.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.video_templates import TEMPLATES
from basivo_orch.flows.templating import render_value

#: Pinned rather than `latest`: the renderer's version is part of what makes a
#: composition reproducible, and a silent upgrade mid-project is a changed
#: video nobody asked for. Override with BASIVO_HYPERFRAMES_BIN.
HYPERFRAMES_PACKAGE = "hyperframes@0.8.3"

#: How long a render may take in total. Generous — a minute of 1080p is real
#: work — but finite, because a browser that never finishes a frame otherwise
#: holds a worker slot forever.
RENDER_TIMEOUT_SECONDS = 900

#: What a composition may declare. Beyond this the wait and the file size stop
#: being reasonable for a queue that also runs everything else.
MAX_DURATION_SECONDS = 120


def hyperframes_command() -> list[str]:
    """How to invoke the renderer here.

    Ordered by how much the operator has said: an explicit binary wins, then a
    `hyperframes` already on PATH (what a worker image installs), and only then
    `npx`, which downloads on first use and is the developer-laptop path.
    """
    explicit = os.environ.get("BASIVO_HYPERFRAMES_BIN", "").strip()
    if explicit:
        return shlex.split(explicit)
    installed = shutil.which("hyperframes")
    if installed:
        return [installed]
    return ["npx", "--yes", HYPERFRAMES_PACKAGE]


class VideoRenderConfig(BaseModel):
    model_config = {"extra": "forbid"}

    template: Literal["product_promo", "announcement", "stat_reveal", "anime_title", "custom"] = (
        "product_promo"
    )
    #: Only read when template is "custom". A full HyperFrames composition.
    html: str = Field(default="", max_length=800_000, description="Your own composition HTML.")
    #: Values for the chosen template, as JSON. Templated, so an agent upstream
    #: can write the copy: {"headline": "{{ nodes.writer.output.text }}"}.
    variables: str = Field(
        default="{}",
        max_length=20_000,
        description='JSON of values, e.g. {"headline": "{{ nodes.copy.output.text }}"}.',
    )
    format: Literal["mp4", "webm", "gif"] = "mp4"
    quality: Literal["draft", "standard", "high"] = "standard"
    fps: int = Field(default=30, ge=1, le=60)
    #: Each worker is a separate Chrome (~256MB). Two is a sane default for a
    #: container that is also running everything else.
    workers: int = Field(default=2, ge=1, le=8)
    filename: str = Field(default="video", max_length=100)

    @model_validator(mode="after")
    def _custom_needs_html(self) -> VideoRenderConfig:
        if self.template == "custom" and not self.html.strip():
            raise ValueError("A custom video needs its composition HTML.")
        return self

    @model_validator(mode="after")
    def _variables_must_be_json(self) -> VideoRenderConfig:
        raw = self.variables.strip()
        if not raw:
            return self
        # `{{ template }}` markers are not JSON until they are rendered, so a
        # value containing one is checked at run time instead of here.
        if "{{" in raw:
            return self
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Variables must be a JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Variables must be a JSON object, not a list or a bare value.")
        return self

    def composition(self) -> str:
        return self.html if self.template == "custom" else TEMPLATES[self.template]


class VideoRenderNode(Node):
    """A composition in, an MP4 out."""

    type = "video.render"
    label = "Render Video"
    description = "Turn an animated HTML composition into an MP4 — templates or your own."
    tier = 2
    category = "design"
    config_model = VideoRenderConfig
    output_paths = ("artifact_id", "url", "duration_seconds", "size_bytes", "format")
    #: One attempt: a retry means another several minutes of the same work, and
    #: a render that failed on the composition will fail again identically.
    max_attempts = 1
    timeout_seconds = float(RENDER_TIMEOUT_SECONDS + 60)

    async def run(self, config: VideoRenderConfig, ctx: NodeContext) -> NodeResult:
        template_context = ctx.template_context()
        variables = _resolve_variables(config.variables, template_context)
        html = str(render_value(config.composition(), template_context))

        duration = _declared_duration(html)
        if duration > MAX_DURATION_SECONDS:
            raise NodeError(
                f"This composition is {duration:g}s and the limit is {MAX_DURATION_SECONDS}s. "
                "Render it in parts, or shorten it."
            )

        await ctx.step(
            "video.started",
            {
                "template": config.template,
                "format": config.format,
                "quality": config.quality,
                "fps": config.fps,
                "duration_seconds": duration,
                "variables": list(variables),
            },
        )
        await ctx.progress(f"Rendering {duration:g}s of {config.format} — this takes a while")

        data, logs = await _render(html, variables=variables, config=config)

        if not data:
            raise NodeError(f"The renderer produced no file. Its last words:\n{logs[-1200:]}")

        saved = await ctx.save_artifact(
            data,
            filename=f"{config.filename}.{config.format}",
            content_type={"mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif"}[
                config.format
            ],
            node_id=ctx.node_id,
        )
        await ctx.step("video.finished", {**saved, "duration_seconds": duration})

        return NodeResult(
            output={**saved, "duration_seconds": duration, "format": config.format},
        )


def _resolve_variables(raw: str, template_context: dict[str, Any]) -> dict[str, Any]:
    """Render `{{ … }}` inside the variables, then parse them as JSON."""
    rendered = str(render_value(raw, template_context)) if raw.strip() else "{}"
    try:
        parsed = json.loads(rendered)
    except ValueError as exc:
        raise NodeError(
            f"The variables did not come out as JSON after filling in references: {exc}. "
            "A value containing quotes or newlines needs escaping — ask the agent for plain text."
        ) from exc
    if not isinstance(parsed, dict):
        raise NodeError("Variables must be a JSON object.")
    return parsed


def _declared_duration(html: str) -> float:
    """The composition's own `data-duration`, or a conservative default."""
    import re

    match = re.search(r'data-duration=["\']([0-9.]+)["\']', html)
    return float(match.group(1)) if match else 10.0


async def _render(
    html: str, *, variables: dict[str, Any], config: VideoRenderConfig
) -> tuple[bytes, str]:
    """Run the renderer in a scratch project. Returns (bytes, combined logs)."""
    command = hyperframes_command()

    with tempfile.TemporaryDirectory(prefix="basivo-video-") as workdir:
        project = Path(workdir)
        (project / "index.html").write_text(html, encoding="utf-8")
        output = project / f"out.{config.format}"

        argv = [
            *command,
            "render",
            str(project),
            "-o",
            str(output),
            "-q",
            config.quality,
            "-f",
            str(config.fps),
            "--format",
            config.format,
            "-w",
            str(config.workers),
            "--quiet",
        ]
        if variables:
            argv += ["--variables", json.dumps(variables)]

        # A stripped environment: the renderer runs somebody's JavaScript, and
        # it has no business seeing SECRET_KEY, DATABASE_URL, or a model key.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", workdir),
            "TMPDIR": workdir,
            "CI": "1",
            "NO_COLOR": "1",
        }

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError as exc:
            raise NodeError(
                "The video renderer is not installed. Install it with "
                f"`npm install -g {HYPERFRAMES_PACKAGE}` (Node 22+ and FFmpeg are required), "
                "or set BASIVO_HYPERFRAMES_BIN to its path."
            ) from exc

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=RENDER_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise NodeError(
                f"The render did not finish within {RENDER_TIMEOUT_SECONDS}s. Shorten the "
                "composition, lower the quality, or reduce the frame rate."
            ) from None

        logs = (stdout or b"").decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise NodeError(
                f"The renderer failed (exit {process.returncode}). Its output:\n{logs[-1500:]}"
            )
        if not output.exists():
            return b"", logs
        return output.read_bytes(), logs
