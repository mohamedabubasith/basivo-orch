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
import re
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
    match = re.search(r'data-duration=["\']([0-9.]+)["\']', html)
    return float(match.group(1)) if match else 10.0


async def _render(
    html: str,
    *,
    variables: dict[str, Any],
    config: VideoRenderConfig,
    assets: dict[str, bytes] | None = None,
) -> tuple[bytes, str]:
    """Run the renderer in a scratch project. Returns (bytes, combined logs)."""
    command = hyperframes_command()

    with tempfile.TemporaryDirectory(prefix="basivo-video-") as workdir:
        project = Path(workdir)
        (project / "index.html").write_text(html, encoding="utf-8")
        # Narration and any other local media the composition refers to by
        # relative name. They have to be real files beside index.html: the
        # renderer mixes audio with ffmpeg from the path on disk, not from
        # whatever the browser managed to play.
        for name, blob in (assets or {}).items():
            (project / Path(name).name).write_bytes(blob)
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
#: Fractions of the composition to look at, for a short clip. Kept for the
#: 6-second case where three points really is the whole video.
PROBE_POINTS = (0.15, 0.5, 0.85)

#: How far apart to look, in seconds, once a composition is long enough for
#: three points to miss things.
PROBE_EVERY_SECONDS = 2.0
#: A ceiling on the sampling. Each point is one JS evaluation on an
#: already-loaded page, so they are cheap — but not free.
MAX_PROBE_POINTS = 16


def probe_moments(duration: float) -> list[float]:
    """Which seconds to inspect.

    Three points caught a composition that was blank from start to finish, and
    missed one that was blank for its last eight seconds — 0.85 of 30s is 25.5s,
    and the dead zone sat between the samples. A 30-second video gets fourteen
    looks instead of three, which is what it takes to notice a gap rather than
    a total failure.

    The last look is deliberately close to the end: the most common dead zone
    is the tail, where the narration has finished and the animation has run
    out of scenes.
    """
    if duration <= 8:
        return [round(duration * fraction, 2) for fraction in PROBE_POINTS]

    count = min(MAX_PROBE_POINTS, max(5, int(duration / PROBE_EVERY_SECONDS)))
    step = duration / (count + 1)
    moments = [round(step * (index + 1), 2) for index in range(count)]
    tail = round(duration * 0.97, 2)
    if tail - moments[-1] > 0.3:
        moments.append(tail)
    return moments


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
    // Opacity does NOT inherit as a computed value: a heading inside a
    // `.clip` at opacity 0 still computes to opacity 1, and reading only its
    // own style passed a composition that rendered thirty seconds of black.
    // So the whole ancestor chain is multiplied out.
    let effective = 1;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const nodeStyle = node === el ? style : getComputedStyle(node);
      if (nodeStyle.visibility === 'hidden' || nodeStyle.display === 'none') { effective = 0; break; }
      effective *= parseFloat(nodeStyle.opacity);
      if (effective <= 0.05) break;
    }
    if (effective > 0.05 && box.width > 0 && box.height > 0) {
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
            for moment in probe_moments(duration):
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

    # -- voice ---------------------------------------------------------------
    #: Narrate the video. The agent writes a script first, it is spoken, and
    #: the animation is then authored to the length the voice actually took —
    #: the other order cuts the tail off every line.
    narration: bool = False
    voice: str = Field(default="af_heart", max_length=40, title="Voice")
    voice_speed: float = Field(default=1.0, ge=0.5, le=2.0, title="Voice speed")
    #: Word-level captions, timed from the model's own phoneme durations.
    #: On by default when narrating: short-form video is mostly watched muted,
    #: so a narrated video without captions says nothing to half its audience.
    captions: bool = True

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
    output_paths = (
        "artifact_id",
        "url",
        "attempts",
        "duration_seconds",
        "preview_artifact_id",
        "narration_artifact_id",
        "script",
        "words",
    )
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

        # The voice comes first. Everything after this — the target duration,
        # the scene timings offered to the agent — is derived from how long the
        # narration actually turned out to be.
        script, narration_audio, spoken_seconds, words = "", b"", 0.0, []
        if config.narration:
            script, narration_audio, spoken_seconds, words = await _narrate(
                config, ctx, model=model, brief=brief, style=style
            )

        target_seconds = (
            round(max(float(config.duration_seconds), spoken_seconds + 0.4), 1)
            if config.narration
            else float(config.duration_seconds)
        )

        instructions = (
            f"{COMPOSITION_INSTRUCTIONS}\n\n"
            f"This composition is {width}x{height}, exactly {target_seconds:g} seconds, "
            f"{config.fps}fps."
        )
        if config.narration:
            instructions += (
                "\n\nA VOICE IS ALREADY RECORDED for this video and will play over it. "
                f"It is {spoken_seconds:g} seconds long. The words, and the second each is "
                "spoken:\n"
                + _spoken_outline(words)
                + "\n\nChange scene ON those moments, not on a round number — a cut that "
                "lands on the word being said is the difference between a video and a "
                "slideshow with sound. Do not add your own text captions at the bottom of "
                "the frame; captions are added after you, and two sets would overlap."
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

        assets: dict[str, bytes] = {}
        narration_id = ""
        if config.narration and narration_audio:
            lines = caption_lines(words) if config.captions else []
            html, widened = ensure_duration(html, spoken_seconds + 0.3)
            if widened:
                # The agent was told the length and wrote something shorter.
                # Rendering it would cut the voice off mid-word.
                await ctx.step(
                    "video.duration_widened",
                    {"to_seconds": round(spoken_seconds + 0.3, 2), "reason": "narration is longer"},
                )
            html, captioned, dropped_audio = inject_narration(
                html,
                audio_name="narration.wav",
                audio_seconds=spoken_seconds,
                lines=lines,
                width=width,
                height=height,
            )
            if dropped_audio:
                # Worth a line in the log: the composition tried to bring its
                # own soundtrack, which would have failed the render outright.
                await ctx.step(
                    "video.audio_replaced",
                    {
                        "dropped": dropped_audio,
                        "note": "The composition declared its own audio; the narration is used.",
                    },
                )
            assets["narration.wav"] = narration_audio
            saved_voice = await ctx.save_artifact(
                narration_audio,
                filename=f"{config.filename}-narration.wav",
                content_type="audio/wav",
                node_id=ctx.node_id,
            )
            narration_id = saved_voice["artifact_id"]
            await ctx.step(
                "video.narration_attached",
                {
                    **saved_voice,
                    "seconds": spoken_seconds,
                    "caption_lines": len(lines),
                    "captions_rendered": captioned,
                },
            )
            if config.captions and not captioned:
                # Said out loud rather than left as a silent difference between
                # what was asked for and what came out.
                await ctx.progress(
                    "Captions were skipped: the composition has no recognisable #stage to "
                    "attach them to. The voice is still in the video."
                )

        await ctx.progress(f"Rendering {target_seconds:g}s of video")
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
            assets=assets,
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
                # The real length, which with narration is the voice's length
                # rounded up — not the number that was asked for.
                "duration_seconds": target_seconds,
                "preview_artifact_id": preview_id,
                "narration_artifact_id": narration_id,
                "script": script,
                "words": words,
                "format": config.format,
            }
        )


def message_text_of(message: Any) -> str:
    from basivo_orch.flows.nodes.agent_runtime import message_text

    return message_text(message)


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------
#
# A silent product video is half a product video, and the hard part is not the
# voice — it is that a scene lasts five seconds while a sentence lasts however
# long the words take. Two orders are possible and only one of them works:
#
#   write the animation, then narrate it  →  the tail of every line gets cut
#   narrate it, then write the animation  →  the animation fits the voice
#
# So the script is written and spoken FIRST, and the composition is authored
# against a duration that is already known — with the word timings handed to the
# agent, so it can change scene on the word rather than on a guess.
#
# HyperFrames does the mixing. An `<audio src>` inside the stage is collected
# into the render's ffmpeg graph (verified: `hasAudio: true`, an AAC stream in
# the output at -22.8 dB mean), so nothing here shells out to mux.

#: The script pass. Deliberately not "write a video" — asking for prose and
#: markup in one reply gets a worse version of both.
NARRATION_INSTRUCTIONS = """You write narration for short product videos: the words a voice
will read aloud, nothing else.

RULES:
1. Stay inside the word range. It is not advice — the voice takes about
   {pace} words per second, so going over means the video ends mid-sentence,
   and coming in far under leaves the end of the video in silence.
2. Short sentences. A clause a listener has to hold in their head does not
   survive being heard once.
3. No stage directions, no scene numbers, no speaker labels, no markdown.
   Every character you write will be spoken out loud, including brackets.
4. Numbers as words where they are read as words ("thirty seconds", not "30s").
5. Open with the thing that matters. A listener decides in two seconds.

Reply with ONLY the narration text."""

#: How many words share one caption line. Six is about a line of large type on
#: a phone in portrait, and short enough that the line changes often enough to
#: feel alive rather than static.
CAPTION_WORDS_PER_LINE = 6


def caption_lines(
    words: list[dict[str, Any]], *, per_line: int = CAPTION_WORDS_PER_LINE
) -> list[dict[str, Any]]:
    """Group timed words into caption lines.

    Broken on sentence endings first and on the word count second, so a line
    never straddles a full stop — a caption that reads "...it yourself. Connect
    a" is harder to read than one that stops where the speaker stopped.
    """
    lines: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        lines.append(
            {
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "words": list(current),
            }
        )
        current.clear()

    for word in words:
        current.append(word)
        ends_sentence = word["word"].rstrip("\"'”’)").endswith((".", "!", "?"))
        if ends_sentence or len(current) >= per_line:
            flush()
    flush()
    return lines


#: A paired `<audio>…</audio>`, tempered so one tag cannot swallow the next:
#: `.*?` across two elements counted them as one and quietly changed what
#: "how many did we drop" means.
_AUDIO_PAIR = re.compile(r"<audio\b[^>]*>(?:(?!</audio\b).)*</audio\s*>", re.DOTALL | re.IGNORECASE)
#: Whatever is left: a self-closed or unclosed tag.
_AUDIO_LONE = re.compile(r"<audio\b[^>]*/?>", re.IGNORECASE)
#: Self-closed tags are removed FIRST, and not only for a tidy count: a
#: `<audio/>` earlier in the document would otherwise act as the opening tag of
#: the next `</audio>`, and everything between them — real composition markup —
#: would be deleted with it.
_AUDIO_SELF = re.compile(r"<audio\b[^>]*/\s*>", re.IGNORECASE)


def strip_audio(html: str) -> tuple[str, int]:
    """Remove `<audio>` elements the composition brought with it.

    Not defensive programming for its own sake — this is the failure it was
    written for: told "a voice is already recorded for this video", an agent
    added `<audio src="voice.mp3">` to be helpful. There is no voice.mp3, and
    the renderer treats a missing media source as a correctness error and
    refuses to produce anything at all. One hallucinated filename, no video.

    So the narration track is ours alone: whatever the composition declared is
    dropped, and the element that actually points at the file we wrote is
    injected afterwards.
    """
    stripped, self_closed = _AUDIO_SELF.subn("", html)
    stripped, paired = _AUDIO_PAIR.subn("", stripped)
    stripped, lone = _AUDIO_LONE.subn("", stripped)
    return stripped, self_closed + paired + lone


def _stage_span(html: str) -> tuple[int, int] | None:
    """Where the root element opens and closes, by counting nested divs.

    A regex for the closing tag would find the first `</div>` in the document,
    which is almost never the stage's — the stage contains every clip.
    """
    opening = re.search(r"<div\b[^>]*id=[\"']stage[\"'][^>]*>", html)
    if not opening:
        return None

    depth = 0
    for match in re.finditer(r"<div\b[^>]*>|</div\s*>", html[opening.start() :]):
        if match.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return opening.end(), opening.start() + match.start()
        else:
            depth += 1
    return None


def composition_id(html: str) -> str:
    match = re.search(r"data-composition-id=[\"']([^\"']+)[\"']", html)
    return match.group(1) if match else ""


def ensure_duration(html: str, seconds: float) -> tuple[str, bool]:
    """Widen the root `data-duration` if the narration outlasts it.

    The agent is told the exact length and mostly honours it. When it does not,
    the render silently cuts the voice off — so the declared duration is
    checked against the audio we actually have, and the audio wins.
    """
    match = re.search(
        r'(<div\b[^>]*id=["\']stage["\'][^>]*?)data-duration=["\']([0-9.]+)["\']', html
    )
    if not match:
        return html, False
    declared = float(match.group(2))
    if declared >= seconds - 0.05:
        return html, False
    return (
        html[: match.start(2)] + f"{seconds:g}" + html[match.end(2) :],
        True,
    )


def narration_markup(
    *,
    audio_name: str,
    audio_seconds: float,
    lines: list[dict[str, Any]],
    width: int,
    height: int,
) -> str:
    """The `<audio>` element, and the caption layer if there are words for it.

    Captions are not decoration: most short-form video is watched with the
    sound off, so a narrated video without them communicates nothing to a
    large part of its audience.
    """
    audio = (
        f'\n<audio src="{audio_name}" data-start="0" '
        f'data-end="{audio_seconds:g}" data-volume="1"></audio>'
    )
    if not lines:
        return audio

    # Sized from the frame rather than fixed, so a 1080x1920 story caption is
    # not the same 40px as a 1920x1080 landscape one.
    font = max(20, round(height * 0.042))
    band = round(height * 0.07)
    # A scrim behind the band, for two reasons. Legibility over a light or busy
    # background is the obvious one. The other was found by looking at a real
    # render: an agent told not to write its own subtitles wrote them anyway,
    # in the same place, and the frame showed both sets of words superimposed
    # into nonsense. The scrim covers whatever sits behind ours, so the worst
    # case is a hidden line rather than an unreadable one.
    parts = [
        audio,
        f'\n<div id="hf-captions" style="position:absolute;left:0;right:0;bottom:0;'
        f"height:{round(font * 3.4)}px;pointer-events:none;z-index:2147483000;"
        f"background:linear-gradient(to top,rgba(0,0,0,0.86) 0%,"
        f'rgba(0,0,0,0.74) 60%,rgba(0,0,0,0) 100%)">',
        f'<div id="hf-caption-text" style="position:absolute;left:6%;right:6%;'
        f"bottom:{band}px;text-align:center;"
        f"font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
        f'font-weight:800;font-size:{font}px;line-height:1.25;letter-spacing:-0.01em">',
    ]
    for index, line in enumerate(lines):
        spans = "".join(
            f'<span id="hf-w-{index}-{position}" style="opacity:.45;color:#fff;'
            f'text-shadow:0 2px 12px rgba(0,0,0,.85),0 0 2px rgba(0,0,0,.9)"> '
            f"{_escape(word['word'])}</span>"
            for position, word in enumerate(line["words"])
        )
        parts.append(
            f'<div id="hf-line-{index}" style="position:absolute;left:0;right:0;'
            f'bottom:0;opacity:0">{spans}</div>'
        )
    parts.append("</div></div>")
    return "".join(parts)


def caption_script(composition: str, lines: list[dict[str, Any]]) -> str:
    """Drive the caption layer from the composition's own timeline.

    Appending to `window.__timelines[id]` rather than using CSS animation,
    because the renderer produces frames by *seeking* that timeline. A CSS
    animation would sit at whatever state it was in when the page loaded and
    every frame would look identical.
    """
    if not lines or not composition:
        return ""

    operations: list[str] = []
    for index, line in enumerate(lines):
        # A line lingers 0.08s past its last word so it does not blink out on
        # the final syllable — but never past the moment the next line appears,
        # or both are drawn on the same frame and the words interleave.
        following = lines[index + 1]["start"] if index + 1 < len(lines) else None
        hide = line["end"] + 0.08
        if following is not None:
            hide = min(hide, following - 0.001)
        operations.append(
            f'tl.set("#hf-line-{index}",{{opacity:1}},{line["start"]:g});'
            f'tl.set("#hf-line-{index}",{{opacity:0}},{hide:g});'
        )
        for position, word in enumerate(line["words"]):
            # The spoken word brightens; the rest of the line stays readable at
            # 45%, which is what makes a caption feel spoken rather than typed.
            operations.append(
                f'tl.set("#hf-w-{index}-{position}",{{opacity:1}},{word["start"]:g});'
            )
    # The timeline may not exist yet: a composition is free to build it on
    # `load` rather than inline, and a caption layer that gave up in that case
    # silently produced a video with a voice and no words on screen. So it
    # waits — and applies once, whichever path gets there first.
    return (
        "\n<script>(function(){var done=false;var key="
        + json.dumps(composition)
        + ";function apply(){if(done)return true;var tl=(window.__timelines||{})[key];"
        "if(!tl||!tl.set)return false;done=true;" + "".join(operations) + "return true;}"
        "if(!apply()){var n=0;var id=setInterval(function(){"
        "if(apply()||++n>150)clearInterval(id);},20);"
        "window.addEventListener('load',apply);"
        "document.addEventListener('DOMContentLoaded',apply);}"
        "})();</script>"
    )


def inject_narration(
    html: str,
    *,
    audio_name: str,
    audio_seconds: float,
    lines: list[dict[str, Any]],
    width: int,
    height: int,
) -> tuple[str, bool, int]:
    """Put the voice and the captions into a finished composition.

    Returns (html, captions_added, audio_elements_dropped). The markup goes at
    the END of the stage so captions paint over the scenes, and the script goes
    after the composition's own so the timeline it appends to already exists.
    """
    html, dropped = strip_audio(html)
    span = _stage_span(html)
    if span is None:
        # No recognisable stage: keep the voice (which needs no anchor) and say
        # in the log that captions were skipped. A silent-but-rendered video is
        # a better outcome than failing the run over a caption layer.
        audio_only = narration_markup(
            audio_name=audio_name, audio_seconds=audio_seconds, lines=[], width=width, height=height
        )
        return html.replace("</body>", audio_only + "\n</body>", 1), False, dropped

    _, closing = span
    markup = narration_markup(
        audio_name=audio_name,
        audio_seconds=audio_seconds,
        lines=lines,
        width=width,
        height=height,
    )
    with_markup = html[:closing] + markup + html[closing:]
    script = caption_script(composition_id(html), lines)
    if script and "</body>" in with_markup:
        with_markup = with_markup.replace("</body>", script + "\n</body>", 1)
    return with_markup, bool(script), dropped


async def _narrate(
    config: Any, ctx: NodeContext, *, model: Any, brief: str, style: str
) -> tuple[str, bytes, float, list[dict[str, Any]]]:
    """Write the script, speak it, and report how long it really took.

    The word budget is given to the agent and then *checked*, because a model
    told "about seventy words" will cheerfully write ninety. Overrunning is not
    a style problem: the video would end while the voice is still talking.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from basivo_orch.flows.nodes.speech import WORDS_PER_SECOND, speak, word_budget

    budget = word_budget(config.duration_seconds)
    # A floor as well as a ceiling. Asked only for a maximum, a model reliably
    # writes well under it — the first 30-second video came back with 51 words
    # of a 75-word budget and ended with nine seconds of silence.
    floor = max(1, int(budget * 0.85))
    system = NARRATION_INSTRUCTIONS.format(pace=WORDS_PER_SECOND)
    ask = (
        f"{brief}\n\n"
        + (f"Art direction (for tone, not for the words): {style}\n\n" if style else "")
        + f"The video is {config.duration_seconds} seconds, so write between {floor} and "
        f"{budget} words. Use the room — a script well under {floor} words leaves the video "
        "silent at the end."
    )
    conversation: list[Any] = [SystemMessage(content=system), HumanMessage(content=ask)]

    script = ""
    for attempt in (1, 2):
        reply = await model.ainvoke(conversation)
        script = strip_code_fences(message_text_of(reply)).strip()
        count = len(script.split())
        await ctx.step(
            "video.script",
            {"attempt": attempt, "words": count, "budget": budget, "script": script[:600]},
        )
        if count <= budget * 1.15 or attempt == 2:
            break
        conversation.append(
            HumanMessage(content=reply.content if hasattr(reply, "content") else script)
        )
        conversation.append(
            HumanMessage(
                content=(
                    f"That is {count} words and the budget is {budget}. It would run past the "
                    f"end of the video. Cut it to {budget} words or fewer, keeping the opening. "
                    "Reply with ONLY the narration."
                )
            )
        )

    if not script:
        raise NodeError("The agent returned an empty narration script.")

    await ctx.progress(f"Speaking {len(script.split())} words as {config.voice}")
    audio, seconds, words = await speak(script, voice=config.voice, speed=config.voice_speed)
    await ctx.step(
        "video.spoken",
        {
            "seconds": seconds,
            "words": len(words),
            "voice": config.voice,
            "words_per_second": round(len(script.split()) / seconds, 2) if seconds else None,
        },
    )
    return script, audio, seconds, words


def _spoken_outline(words: list[dict[str, Any]], *, every: int = 3) -> str:
    """The narration as a timing sheet the agent can cut against.

    Every word would be thousands of characters of prompt for a 30-second
    script and more precision than a scene change needs; every third word is
    enough to place a cut within a third of a second.
    """
    if not words:
        return ""
    picked = [
        f"{word['start']:.1f}s {word['word']}"
        for index, word in enumerate(words)
        if index % every == 0
    ]
    last = words[-1]
    picked.append(f"{last['end']:.1f}s (end)")
    return "  ".join(picked)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
