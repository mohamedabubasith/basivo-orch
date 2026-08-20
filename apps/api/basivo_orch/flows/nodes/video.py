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


#: What an agent must be told to produce a composition that renders.
#:
#: Shipped as part of the product rather than left for each user to rediscover:
#: every rule here corresponds to a way a model-written composition fails, and
#: the worst of them (no exposed timeline) fails *silently* as a still image.
COMPOSITION_INSTRUCTIONS = """You write HyperFrames video compositions: one HTML file that a
headless browser renders frame by frame into MP4.

THE CONTRACT — breaking any of these renders wrong, or not at all:
1. One root element:
   <div id="stage" data-composition-id="promo" data-start="0"
        data-duration="<seconds>" data-width="1920" data-height="1080" data-fps="30">
   #stage must set overflow:hidden and those exact pixel dimensions.
2. Content lives in elements with class="clip" plus data-start, data-duration
   and data-track-index.
3. Load GSAP from https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js
4. Build ONE paused timeline and expose it — without this the video is a still
   image and nothing says why:
     const tl = gsap.timeline({ paused: true });
     tl.from('#a', { opacity: 0, y: 40, duration: .8 }, 0.2);
     window.__timelines = window.__timelines || {};
     window.__timelines['promo'] = tl;
   Every tween needs an explicit position argument (the number after the vars).
5. Everything inline. No external CSS, no images, no web fonts — system fonts,
   CSS gradients and shapes only. Anything fetched may not arrive in time.
6. Several scenes are made by animating the opacity and position of separate
   .clip elements at different times. Never by changing the DOM.

STYLE: high contrast, large type, generous spacing, one idea per scene.

Reply with ONLY the HTML document. No markdown fences, no commentary."""

#: What a composition must contain to render as video rather than a still.
_REQUIRED = (
    ("data-composition-id", 'a root <div id="stage" data-composition-id=...>'),
    ("data-duration", "data-duration on the root element"),
    ("window.__timelines", "the paused GSAP timeline exposed on window.__timelines"),
)


def strip_code_fences(text: str) -> str:
    """Unwrap ```html … ``` if a model wrapped its answer.

    Models are told not to, and do it anyway perhaps one time in five.
    Rendering a fence produces a video of the literal characters ```html,
    which is a baffling thing to receive.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def composition_problems(html: str) -> list[str]:
    """What is missing from this composition, in words a person can act on.

    Checked before rendering because the alternative is discovering it after
    several minutes of CPU — and because the commonest failure, a composition
    with no exposed timeline, renders *successfully* as a motionless video.
    """
    return [f"missing {description}" for marker, description in _REQUIRED if marker not in html]


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
        html = strip_code_fences(str(render_value(config.composition(), template_context)))

        if problems := composition_problems(html):
            raise NodeError(
                "This composition will not render as video — "
                + "; ".join(problems)
                + ". It needs a root #stage with data-duration and a paused GSAP timeline on "
                "window.__timelines. If an agent wrote it, put the composition instructions "
                "in that agent's system prompt."
            )

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


# ---------------------------------------------------------------------------
# video.generate — the agent writes, we check, it fixes, then we render
# ---------------------------------------------------------------------------
#
# One node rather than two wired together, because the interesting part is the
# loop: a model cannot see what it wrote, so left alone it hands over a
# composition that renders successfully as six seconds of empty gradient — the
# worst outcome, since nothing failed. Between the model and the renderer this
# node opens the composition in a browser, seeks the timeline, and asks the
# page what is actually visible. If nothing is, the model is told so and tries
# again. That check costs about a second; a wasted render costs minutes.

#: When to look. Early, middle and late catches the common failures: a scene
#: that never appears, one that leaves and never returns, an empty ending.
PROBE_POINTS = (0.15, 0.5, 0.85)

#: Asks the page what a viewer would actually see at time `t`.
_PROBE_JS = """(t) => {
  const timelines = window.__timelines || {};
  const timeline = Object.values(timelines)[0];
  if (timeline && timeline.seek) timeline.seek(t);
  const seen = [];
  for (const el of document.querySelectorAll('h1,h2,h3,h4,p,span,div,li')) {
    const first = el.childNodes.length ? el.childNodes[0].nodeValue : '';
    const text = (first || '').trim();
    if (!text) continue;
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    if (parseFloat(style.opacity) > 0.05 && style.visibility !== 'hidden'
        && box.width > 0 && box.height > 0) {
      seen.push(text.slice(0, 60));
    }
  }
  return seen;
}"""


async def probe_composition(
    html: str, *, width: int, height: int, duration: float
) -> tuple[dict[float, list[str]], list[str]]:
    """What is visible at a few moments, and any JavaScript that broke.

    Returns ({time: [visible text]}, [js errors]). A composition whose script
    threw has no timeline at all, which is worth saying in those words rather
    than reporting as "nothing visible".
    """
    from playwright.async_api import async_playwright

    errors: list[str] = []
    visible: dict[float, list[str]] = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            page = await (
                await browser.new_context(viewport={"width": width, "height": height})
            ).new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)[:200]))
            await page.set_content(html, wait_until="load")
            # GSAP arrives from a CDN; the timeline does not exist until it has.
            await page.wait_for_timeout(900)
            for fraction in PROBE_POINTS:
                moment = round(duration * fraction, 2)
                try:
                    visible[moment] = await page.evaluate(_PROBE_JS, moment)
                except Exception as exc:  # noqa: BLE001 — a broken page is data
                    errors.append(str(exc)[:200])
                    visible[moment] = []
        finally:
            await browser.close()

    return visible, errors


def review(html: str, visible: dict[float, list[str]], errors: list[str]) -> list[str]:
    """Everything wrong with this composition, phrased for the model that wrote it."""
    problems = composition_problems(html)
    problems += [f"JavaScript error: {error}" for error in errors]

    empty = [str(moment) for moment, texts in visible.items() if not texts]
    if len(empty) == len(visible) and visible:
        problems.append(
            "nothing at all is visible at any point — the most likely cause is animating "
            "`from` a value the element already has (tl.from(el, {opacity: 0}) when the CSS "
            "already sets opacity: 0 animates from 0 to 0). Set the resting state to visible "
            "and animate `from` the hidden state, or use tl.fromTo with both ends explicit"
        )
    elif empty:
        problems.append(
            f"nothing is visible at {', '.join(empty)}s — a scene either never appears or "
            "leaves a gap. Every moment of the video should show something"
        )
    return problems


class VideoGeneratorConfig(BaseModel):
    """Describe the video; the agent writes it and this node checks its work."""

    model_config = {"extra": "forbid"}

    brief: str = Field(
        min_length=1,
        max_length=8000,
        description="What the video should say and feel like. Supports {{ references }}.",
    )
    style: str = Field(
        default="",
        max_length=2000,
        description="Art direction: colours, mood, brand. Optional.",
    )
    duration_seconds: int = Field(default=6, ge=2, le=60)
    size: Literal["landscape", "square", "story"] = "landscape"

    provider: str = Field(default="openai", max_length=48)
    model: str = Field(default="", max_length=160)
    credential_id: str = Field(default="", description="A saved model credential.")

    #: How many times the agent may revise before this gives up. Each round is
    #: one model call plus about a second of checking — cheap next to a render.
    max_attempts: int = Field(default=3, ge=1, le=6)
    format: Literal["mp4", "webm", "gif"] = "mp4"
    quality: Literal["draft", "standard", "high"] = "standard"
    fps: int = Field(default=30, ge=1, le=60)
    filename: str = Field(default="video", max_length=100)
    #: Keep a still from the accepted composition, so the run shows what was
    #: made without anyone downloading the video.
    save_preview: bool = True

    def dimensions(self) -> tuple[int, int]:
        return {"landscape": (1920, 1080), "square": (1080, 1080), "story": (1080, 1920)}[self.size]


class VideoGeneratorNode(Node):
    """Brief in, finished video out — with the agent's revisions on the log."""

    type = "video.generate"
    label = "Video Generator"
    description = "An agent writes the animation, this checks it renders, then makes the MP4."
    tier = 2
    category = "design"
    config_model = VideoGeneratorConfig
    output_paths = ("artifact_id", "url", "attempts", "duration_seconds", "preview_artifact_id")
    max_attempts = 1
    timeout_seconds = float(RENDER_TIMEOUT_SECONDS + 300)

    async def run(self, config: VideoGeneratorConfig, ctx: NodeContext) -> NodeResult:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        from basivo_orch.flows.nodes.models import build_chat_model

        template_context = ctx.template_context()
        brief = str(render_value(config.brief, template_context))
        style = str(render_value(config.style, template_context)) if config.style else ""
        width, height = config.dimensions()

        model = await build_chat_model(
            ctx,
            provider=config.provider,
            model=config.model,
            credential_id=config.credential_id,
            max_tokens=8000,
            temperature=0.4,
        )

        instructions = (
            f"{COMPOSITION_INSTRUCTIONS}\n\n"
            f"This composition is {width}x{height}, exactly {config.duration_seconds} seconds, "
            f"{config.fps}fps."
        )
        conversation: list[Any] = [
            SystemMessage(content=instructions),
            HumanMessage(content=brief + (f"\n\nArt direction: {style}" if style else "")),
        ]

        await ctx.step(
            "video.brief",
            {
                "model": config.model,
                "size": config.size,
                "duration_seconds": config.duration_seconds,
                "max_attempts": config.max_attempts,
            },
        )

        html = ""
        accepted = False
        for attempt in range(1, config.max_attempts + 1):
            await ctx.progress(f"Attempt {attempt}: writing the animation")
            reply = await model.ainvoke(conversation)
            html = strip_code_fences(message_text_of(reply))

            problems = review(
                html,
                *await probe_composition(
                    html, width=width, height=height, duration=float(config.duration_seconds)
                ),
            )
            await ctx.step(
                "video.attempt",
                {"attempt": attempt, "characters": len(html), "problems": problems},
            )

            if not problems:
                accepted = True
                break

            await ctx.progress(f"Attempt {attempt} had {len(problems)} problem(s) — revising")
            conversation.append(AIMessage(content=html))
            conversation.append(
                HumanMessage(
                    content=(
                        "That composition does not work. Problems found by rendering it:\n"
                        + "\n".join(f"- {problem}" for problem in problems)
                        + "\n\nRewrite the whole composition, fixing these. "
                        "Reply with ONLY the HTML."
                    )
                )
            )

        if not accepted:
            raise NodeError(
                f"The agent could not produce a working composition in {config.max_attempts} "
                "attempts. The last problems were logged on this run — raising the attempt "
                "limit or simplifying the brief usually helps."
            )

        preview_id = ""
        if config.save_preview:
            # Aliased, not imported as `_render`: a local import inside this
            # branch shadows the module-level `_render` for the WHOLE function,
            # so with save_preview off the render below hit an unbound local.
            from basivo_orch.flows.nodes.design import RenderConfig
            from basivo_orch.flows.nodes.design import _render as render_still

            frame = await render_still(
                html,
                width=width,
                height=height,
                config=RenderConfig(
                    html="x",
                    size="custom",
                    width=width,
                    height=height,
                    scale=1,
                    wait_for_fonts=False,
                ),
            )
            saved_preview = await ctx.save_artifact(
                frame,
                filename=f"{config.filename}-preview.png",
                content_type="image/png",
                node_id=ctx.node_id,
            )
            preview_id = saved_preview["artifact_id"]
            await ctx.step("video.preview", saved_preview)

        await ctx.progress(f"Rendering {config.duration_seconds}s of video")
        data, logs = await _render(
            html,
            variables={},
            config=VideoRenderConfig(
                template="custom",
                html=html,
                format=config.format,
                quality=config.quality,
                fps=config.fps,
                filename=config.filename,
            ),
        )
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
        await ctx.step("video.finished", {**saved, "attempts": attempt})

        return NodeResult(
            output={
                **saved,
                "attempts": attempt,
                "duration_seconds": float(config.duration_seconds),
                "preview_artifact_id": preview_id,
                "format": config.format,
            }
        )


def message_text_of(message: Any) -> str:
    from basivo_orch.flows.nodes.agent_runtime import message_text

    return message_text(message)
