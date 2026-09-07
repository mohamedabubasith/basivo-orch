"""Video, rendered with Remotion.

A composition is a React component. The author writes one file that
default-exports it; this module puts it inside a project whose dependencies
are already installed, checks a few frames, and encodes.

Three things are deliberately taken away from the author, because each one is
a way videos used to break silently:

**Length, narration and captions belong to the product.** They are assembled
around the author's component in the renderer project, not spliced into what
the author wrote. A composition can no longer end before the voice does, and
an author who adds captions anyway cannot end up with two sets on screen.

**Frames are looked at before anything is encoded.** A composition that
compiles and renders eight seconds of empty gradient is the worst outcome
there is, because nothing failed. Three small stills cost about a second;
a wasted render costs minutes.

**There is no network inside a render.** The subprocess gets a stripped
environment and the composition may only use files this node put beside it.
A model writing `<Img src="https://...">` would otherwise produce a video with
a hole in it, and only at the end.

Two consequences worth stating plainly, unchanged from the previous renderer:
this executes JavaScript that a model wrote, at the same trust level as the
Code node; and video is slow and large, so duration and resolution are capped
here rather than discovered when a worker runs out of memory.

Licence: Remotion is source-available, free for individuals and organisations
of up to three people, and paid above that. See docs/video.md.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.nodes.remotion import (
    MAX_DURATION_SECONDS,
    RENDER_TIMEOUT_SECONDS,
    RenderJob,
    probe,
    render,
)
from basivo_orch.flows.nodes.video_templates import TEMPLATES
from basivo_orch.flows.templating import render_value

#: The sizes people actually publish at, named by where they go rather than by
#: their numbers. Somebody making a story does not think "1080 by 1920".
SIZES: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),
    "square": (1080, 1080),
    "story": (1080, 1920),
}

SIZE_LABELS = {
    "landscape": "Landscape, YouTube or a website (1920 x 1080)",
    "square": "Square, Instagram post (1080 x 1080)",
    "story": "Vertical, Story, Reel or Short (1080 x 1920)",
}


def strip_code_fences(text: str) -> str:
    """Take the code out of a model's reply.

    Models wrap code in fences roughly half the time, whatever the prompt
    says, and a fence in the first line of a composition is a syntax error.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def message_text_of(message: Any) -> str:
    """The text of a model reply, whichever shape the provider used.

    Anthropic and Gemini answer with a list of content blocks; OpenAI answers
    with a string. Reading `.content` alone gives `[{'type': 'text', ...}]` as
    a repr, which then fails to compile as a composition with an error that
    mentions none of this.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Reading a composition before it costs anything
# ---------------------------------------------------------------------------

#: What a composition may import. Everything else is either unavailable inside
#: the bundle or a way to reach the network, and a model reaches for both.
ALLOWED_IMPORTS = {"react", "remotion", "react/jsx-runtime", "react-dom"}

#: Elements the product owns. An author who adds one gets two of them on
#: screen, or a missing file that fails the render outright: told "a voice is
#: already recorded", a model helpfully added its own audio tag pointing at a
#: file that does not exist.
RESERVED_ELEMENTS = ("<Audio", "<Video", "<OffthreadVideo", "<IFrame")

#: One of these has to appear, or the composition is a still image that took
#: several minutes to encode.
MOTION_HOOKS = ("useCurrentFrame", "spring(", "interpolate(", "<Sequence", "<Series")

_IMPORT = re.compile(r"""(?:from|import)\s+["']([^"']+)["']""")
_STATIC_FILE = re.compile(r"""staticFile\(\s*["']([^"']+)["']\s*\)""")
_URL = re.compile(r"""["'](https?://[^"']+)["']""")


def scene_problems(scene: str, *, assets: set[str] | None = None) -> list[str]:
    """Everything wrong with a composition that can be seen without rendering.

    Each check here is a failure that has actually happened, and each message
    is written to be handed straight back to the model that wrote the file.
    """
    problems: list[str] = []
    text = scene.strip()

    if not text:
        return ["There is no composition at all."]

    if "export default" not in text:
        problems.append(
            "There is no default export. The file must end with a component exported as "
            "`export default function Scene(props) { ... }`."
        )

    for module in sorted({name for name in _IMPORT.findall(text)}):
        base = module.split("/")[0] if not module.startswith(".") else module
        if module.startswith("."):
            problems.append(
                f"It imports {module!r}. There is only one file, so it cannot import another."
            )
        elif module not in ALLOWED_IMPORTS and base not in ALLOWED_IMPORTS:
            problems.append(
                f"It imports {module!r}, which is not available. Only 'react' and 'remotion' "
                "can be imported."
            )

    for element in RESERVED_ELEMENTS:
        if element in text:
            problems.append(
                f"It uses {element}>. Audio and video are added around the composition, so "
                "remove it and write only the visuals."
            )

    if not any(marker in text for marker in MOTION_HOOKS):
        problems.append(
            "Nothing in it moves. Drive every position, scale and opacity from "
            "useCurrentFrame(), or the result is a still image."
        )

    if urls := _URL.findall(text):
        problems.append(
            f"It points at {urls[0]}. There is no network during a render, so anything "
            "loaded from a URL renders as an empty space."
        )

    if assets is not None:
        wanted = set(_STATIC_FILE.findall(text))
        if missing := sorted(wanted - assets):
            offer = ", ".join(sorted(assets)) or "none"
            problems.append(
                f"It asks for {missing[0]!r}, which does not exist. The files available are: "
                f"{offer}."
            )

    return problems


# ---------------------------------------------------------------------------
# Looking at the frames
# ---------------------------------------------------------------------------
#
# A composition can compile, render, and be worthless: an animation that
# starts after the video ends, a scene that never becomes opaque, text the
# same colour as the background it sits on. None of that fails. So a handful
# of frames are rendered small and inspected, and the model is told what was
# actually on screen rather than being asked to imagine it.

#: Below this, a frame is one flat colour: nothing has been drawn, or
#: everything drawn is still invisible.
FLAT_FRAME_STDDEV = 4.0

#: Below this, two frames are the same picture. Compression noise and a
#: gradient that shifted by a pixel both sit under it.
IDENTICAL_FRAME_DIFFERENCE = 1.2

#: How many frames to look at. Three catches the common failures (nothing at
#: the start, nothing in the middle, an empty ending) and costs about a
#: second; more would be a second each for a verdict that rarely changes.
PROBE_POINTS = (0.12, 0.5, 0.88)


def probe_frames(duration_seconds: float, fps: int) -> list[int]:
    """Which frames to look at, spread across the video."""
    last = max(0, round(duration_seconds * fps) - 1)
    return sorted({min(last, max(0, round(last * point))) for point in PROBE_POINTS})


def _grey(png: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("L")


def frame_spread(png: bytes) -> float:
    """How much the pixels in a frame differ from each other.

    Zero is a single flat colour. A frame with type on it is well above the
    threshold; a gradient alone sits just under it, which is exactly the case
    worth catching.
    """
    from PIL import ImageStat

    return float(ImageStat.Stat(_grey(png)).stddev[0])


def frame_difference(first: bytes, second: bytes) -> float:
    """How much two frames differ, on average, per pixel.

    Compared at a small size on purpose: the question is whether the picture
    changed, not whether a pixel did, and a thumbnail answers it in a
    millisecond.
    """
    from PIL import ImageChops, ImageStat

    a = _grey(first).resize((64, 36))
    b = _grey(second).resize((64, 36))
    return float(ImageStat.Stat(ImageChops.difference(a, b)).mean[0])


def review_frames(frames: list[bytes], *, seconds: list[float]) -> list[str]:
    """What is wrong with what the video actually shows."""
    problems: list[str] = []
    if not frames:
        return ["Nothing could be rendered from this composition."]

    spreads = [frame_spread(frame) for frame in frames]
    blank_at = [
        seconds[index] if index < len(seconds) else float(index)
        for index, spread in enumerate(spreads)
        if spread < FLAT_FRAME_STDDEV
    ]
    if len(blank_at) == len(frames):
        problems.append(
            "Every frame is a flat colour. Nothing is drawn, or everything drawn is still "
            "invisible when the video is over. Make the first element visible within the "
            "first half second."
        )
    elif blank_at:
        moments = ", ".join(f"{moment:.1f}s" for moment in blank_at)
        problems.append(
            f"The frame at {moments} is empty. Something must be on screen at every moment, "
            "so overlap the scenes rather than leaving a gap between them."
        )

    if len(frames) > 1:
        changes = [frame_difference(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
        if max(changes) < IDENTICAL_FRAME_DIFFERENCE:
            problems.append(
                "The picture never changes. This is a still image with a running time. "
                "Move, fade or scale something across the whole length of the video."
            )

    return problems


# ---------------------------------------------------------------------------
# video.render — a composition in, an MP4 out
# ---------------------------------------------------------------------------


class VideoRenderConfig(BaseModel):
    model_config = {"extra": "forbid"}

    template: Literal[
        "brand_intro",
        "product_demo",
        "workflow_explainer",
        "feature_launch",
        "announcement",
        "stat_reveal",
        "quote",
        "title_card",
        "custom",
    ] = Field(default="feature_launch", title="Template")
    #: Only read when the template is "custom": a React component, exported as
    #: the default, using nothing but react and remotion.
    scene: str = Field(
        default="",
        max_length=400_000,
        title="Your own composition",
        description="A React component, exported as the default. Only used for a custom video.",
    )
    #: Values the composition reads as props. Templated, so an upstream node
    #: can write the copy: {"headline": "{{ nodes.writer.output.text }}"}.
    props: str = Field(
        default="",
        max_length=40_000,
        title="Values",
        description='JSON of values, e.g. {"headline": "{{ nodes.writer.output.text }}"}.',
    )
    #: Artifact ids of images the composition may show, in order. They arrive
    #: beside the composition as p0.png, p1.png and so on.
    images: str = Field(
        default="",
        title="Images",
        max_length=4_000,
        description="Artifact ids of images to use, newest first. They become p0.png, p1.png.",
    )
    size: Literal["landscape", "square", "story"] = Field(
        default="landscape", title="Size", json_schema_extra={"x-enum-labels": SIZE_LABELS}
    )
    duration_seconds: float = Field(default=0, ge=0, le=MAX_DURATION_SECONDS, title="Length")
    format: Literal["mp4", "webm", "gif"] = "mp4"
    quality: Literal["draft", "standard", "high"] = "standard"
    fps: int = Field(default=30, ge=1, le=60)
    background: str = Field(default="#0b1020", max_length=32, title="Background")
    filename: str = Field(default="video", max_length=100)

    @model_validator(mode="after")
    def _custom_needs_a_composition(self) -> VideoRenderConfig:
        if self.template == "custom" and not self.scene.strip():
            raise ValueError("A custom video needs its composition.")
        return self

    @model_validator(mode="after")
    def _props_must_be_json(self) -> VideoRenderConfig:
        raw = self.props.strip()
        if not raw or "{{" in raw:
            # A value holding a `{{ reference }}` is not JSON until it has been
            # filled in, so it is checked at run time instead.
            return self
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Values must be a JSON object: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Values must be a JSON object, not a list or a bare value.")
        return self

    def dimensions(self) -> tuple[int, int]:
        return SIZES[self.size]


class VideoRenderNode(Node):
    """A composition in, a video file out."""

    type = "video.render"
    label = "Make a Video"
    description = "Render a template, or your own React composition, into a video file."
    when = (
        "You know what the video should look like and only the words change. Pick a template, "
        "wire the copy in from an earlier node, and this renders it every time it runs."
    )
    needs = ("A trigger before it, or any node whose output it should work on",)
    example = "Schedule -> Write with AI -> Make a Video -> Post to Social"
    tier = 2
    category = "design"
    config_model = VideoRenderConfig
    output_paths = ("artifact_id", "url", "duration_seconds", "size_bytes", "format")

    #: A browser drawing every frame, then an encode. The heaviest thing this
    #: product does.
    heavy: ClassVar[bool] = True
    #: One attempt: a retry is another several minutes of identical work, and a
    #: composition that failed will fail the same way again.
    max_attempts = 1
    timeout_seconds = float(RENDER_TIMEOUT_SECONDS + 60)

    async def run(self, config: VideoRenderConfig, ctx: NodeContext) -> NodeResult:
        template_context = ctx.template_context()
        chosen = TEMPLATES.get(config.template)

        scene = (
            strip_code_fences(str(render_value(config.scene, template_context)))
            if config.template == "custom"
            else chosen.scene
        )
        props = _resolve_props(config.props, template_context)
        if chosen is not None:
            # The template's own example values fill any gap, so a template
            # rendered with nothing configured is still a finished video rather
            # than a frame of undefined.
            props = {**chosen.props, **props}

        duration = config.duration_seconds or (chosen.duration_seconds if chosen else 8.0)
        background = config.background or (chosen.background if chosen else "#0b1020")
        width, height = config.dimensions()

        assets = await _load_images(config.images, ctx)
        if problems := scene_problems(scene, assets=set(assets)):
            raise NodeError(
                "This composition will not render: "
                + " ".join(problems)
                + (
                    " If an agent wrote it, put the composition rules in that agent's prompt."
                    if config.template == "custom"
                    else ""
                )
            )

        await ctx.step(
            "video.started",
            {
                "template": config.template,
                "format": config.format,
                "quality": config.quality,
                "fps": config.fps,
                "size": config.size,
                "duration_seconds": duration,
                "images": len(assets),
                "props": sorted(props),
            },
        )
        await ctx.progress(f"Rendering {duration:g}s of {config.format}. This takes a while")

        job = RenderJob(
            scene_tsx=scene,
            width=width,
            height=height,
            fps=config.fps,
            duration_seconds=duration,
            props=props,
            background=background,
            fmt=config.format,
            quality=config.quality,
            assets=assets,
        )
        data, info = await render(job)
        if not data:
            raise NodeError("The renderer finished but produced no file.")

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
            output={
                **saved,
                "duration_seconds": duration,
                "format": config.format,
                "width": info.get("width", width),
                "height": info.get("height", height),
            }
        )


def _resolve_props(raw: str, template_context: dict[str, Any]) -> dict[str, Any]:
    """Fill in `{{ references }}`, then read the result as JSON."""
    if not raw.strip():
        return {}
    rendered = str(render_value(raw, template_context))
    try:
        parsed = json.loads(rendered)
    except ValueError as exc:
        raise NodeError(
            f"The values did not come out as JSON after filling in the references: {exc}. "
            "A value containing quotes or newlines needs escaping. Ask the agent that wrote "
            "it for plain text."
        ) from exc
    if not isinstance(parsed, dict):
        raise NodeError("The values must be a JSON object, not a list or a bare value.")
    return parsed


#: How many images one video may use. Past this the render is long enough that
#: the wait stops being reasonable, and the composition stops being readable.
MAX_IMAGES = 12


async def _load_images(reference: str, ctx: NodeContext) -> dict[str, bytes]:
    """Fetch the images a composition may show, named p0, p1, p2.

    Named by position rather than by artifact id, because the composition has
    to be written before the ids are known, and a model asked to remember a
    UUID will invent one.
    """
    if not reference.strip():
        return {}

    from basivo_orch.flows.nodes.montage import _photo_ids

    wanted = _photo_ids(render_value(reference, ctx.template_context()))
    images: dict[str, bytes] = {}
    for index, artifact_id in enumerate(wanted[:MAX_IMAGES]):
        if ctx.load_artifact and (blob := await ctx.load_artifact(artifact_id)):
            images[f"p{index}.png"] = blob
    await ctx.step("video.images", {"count": len(images), "asked_for": len(wanted)})
    return images


# ---------------------------------------------------------------------------
# video.generate — the agent writes, we look, it fixes, then we render
# ---------------------------------------------------------------------------
#
# One node rather than two wired together, because the interesting part is the
# loop. A model cannot see what it wrote, so left alone it hands over a
# composition that renders successfully as eight seconds of empty gradient.
# Between the model and the encode this node renders three small frames and
# looks at them, and tells the model what was actually on screen.

COMPOSITION_INSTRUCTIONS = """You write Remotion compositions: one React
component that renders a video, frame by frame.

Reply with ONLY the code. No explanation, no markdown fence.

THE SHAPE, exactly:

    import React from "react";
    import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate,
            spring, Sequence, Img, staticFile, Easing} from "remotion";

    export default function Scene({headline}) {
      const frame = useCurrentFrame();
      const {fps, durationInFrames, width, height} = useVideoConfig();
      ...
      return <AbsoluteFill style={{...}}>...</AbsoluteFill>;
    }

RULES, each of which is a way a video comes out broken:

1. Import from "react" and "remotion" and nothing else. There is no other
   package, and no network.
2. Everything is a function of `frame`. A CSS animation or a transition does
   not exist here: every frame is drawn on its own, so anything not derived
   from `frame` is frozen for the whole video.
3. Read every size from useVideoConfig(). Never write a pixel size that
   assumes 1920 by 1080; the same composition is rendered vertically.
4. Derive timings from durationInFrames, never from a number of seconds you
   assumed. A composition asked for 20 seconds that animates for 6 leaves 14
   seconds of nothing.
5. Something is visible from frame 0 and something is still moving at the end.
   Fade the first element in over the first 12 frames, not after a second.
6. No <Audio>, <Video> or <OffthreadVideo>, and no captions. Narration and
   subtitles are added around your composition. Adding your own gives the
   viewer two of them.
7. Images, when you are given them, are shown with
   <Img src={staticFile("p0.png")} /> using exactly the names you are given.
   Any other name renders as nothing at all.
8. Fonts: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial,
   sans-serif. There are no web fonts.
9. Type is large. A headline is at least height/12; body text at least
   height/26. This is watched on a phone.
10. Contrast is not optional: light type on a dark ground or the reverse,
    never mid-grey on mid-grey.

USEFUL PATTERNS:

    const enter = interpolate(frame, [0, 12], [0, 1], {extrapolateRight: "clamp"});
    const pop = spring({frame: frame - 10, fps, config: {damping: 14}});
    <Sequence from={fps * 2} durationInFrames={fps * 3}>...</Sequence>
    const drift = interpolate(frame, [0, durationInFrames], [1.05, 1.15]);"""

NARRATION_INSTRUCTIONS = """You write narration for short product videos: the words a voice
will read aloud, nothing else.

RULES:
1. Stay inside the word range. It is not advice, because the voice takes about
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
    never straddles a full stop: a caption reading "...it yourself. Connect a"
    is harder to read than one that stops where the speaker stopped.
    """
    lines: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        lines.append(
            {
                "text": " ".join(str(word["word"]).strip() for word in current).strip(),
                "from": float(current[0]["start"]),
                "to": float(current[-1]["end"]),
            }
        )
        current.clear()

    for word in words:
        current.append(word)
        ends_sentence = str(word["word"]).rstrip("\"'”’)").endswith((".", "!", "?"))
        if ends_sentence or len(current) >= per_line:
            flush()
    flush()
    return lines


def _spoken_outline(words: list[dict[str, Any]], *, every: int = 3) -> str:
    """The narration as a timing sheet the composition can cut against.

    Every word would be thousands of characters of prompt for a 30-second
    script and more precision than a scene change needs; every third word
    places a cut within a third of a second.
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
    duration_seconds: int = Field(default=8, ge=2, le=MAX_DURATION_SECONDS)
    size: Literal["landscape", "square", "story"] = Field(
        default="landscape", title="Size", json_schema_extra={"x-enum-labels": SIZE_LABELS}
    )

    #: Images the composition may use, as artifact ids or a reference, usually
    #: {{ trigger.photo_ids }} or the output of an earlier node. Without these
    #: an agent-written video can only be type and colour, which for a product
    #: demo is the whole thing missing.
    photos: str = Field(default="", title="Images", max_length=4_000)

    provider: str = Field(default="openai", max_length=48)
    model: str = Field(default="", max_length=160)
    credential_id: str = Field(
        default="", title="Model credential", description="The saved key the model is called with."
    )

    #: How many times the agent may revise before this gives up. Each round is
    #: one model call plus a few seconds of looking, which is cheap next to a
    #: render.
    max_attempts: int = Field(default=3, ge=1, le=6)
    format: Literal["mp4", "webm", "gif"] = "mp4"
    quality: Literal["draft", "standard", "high"] = "standard"
    fps: int = Field(default=30, ge=1, le=60)
    background: str = Field(default="#0b1020", max_length=32, title="Background")
    filename: str = Field(default="video", max_length=100)
    #: Keep a still from the accepted composition, so the run shows what was
    #: made without anyone downloading the video.
    save_preview: bool = True

    # -- voice ---------------------------------------------------------------
    narration: bool = Field(
        default=False,
        title="Add a voice-over",
        description=(
            "The agent writes a short script from the brief, a voice reads it, and the "
            "video is made as long as the voice actually took. No speech key needed."
        ),
    )
    voice: str = Field(default="af_heart", max_length=40, title="Voice")
    voice_speed: float = Field(default=1.0, ge=0.5, le=2.0, title="Voice speed")
    captions: bool = Field(
        default=True,
        title="Captions",
        description=(
            "Word-timed captions when a voice-over is on. Most short video is watched muted."
        ),
    )

    def dimensions(self) -> tuple[int, int]:
        return SIZES[self.size]


class VideoGeneratorNode(Node):
    """Brief in, finished video out, with the agent's revisions on the log."""

    type = "video.generate"
    label = "Describe a Video"
    description = "Describe the video you want; an agent writes the animation and this renders it."
    when = (
        "The video is different every time: an intro for a site, a demo of a feature, a clip "
        "from whatever the flow just produced. Describe it in words and get a file back."
    )
    needs = (
        (
            "An LLM credential (OpenAI, Anthropic, Gemini, Groq or another provider) saved under "
            "Credentials"
        ),
        "Images from the trigger or an earlier node, when the video should show them.",
    )
    example = "Schedule -> Write with AI -> Describe a Video -> Post to Social"
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
    #: Speech, a few frames per attempt, a still, then the render.
    heavy: ClassVar[bool] = True
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

        # The voice comes first. The length of the video, and the moments the
        # composition is asked to cut on, are both derived from how long the
        # narration actually turned out to be.
        script, narration_audio, spoken_seconds, words = "", b"", 0.0, []
        if config.narration:
            script, narration_audio, spoken_seconds, words = await _narrate(
                config, ctx, model=model, brief=brief, style=style
            )

        duration = (
            round(max(float(config.duration_seconds), spoken_seconds + 0.4), 1)
            if config.narration
            else float(config.duration_seconds)
        )

        assets = await _load_images(config.photos, ctx)
        instructions = (
            f"{COMPOSITION_INSTRUCTIONS}\n\n"
            f"This video is {width} by {height}, {duration:g} seconds, {config.fps} frames "
            f"per second, on a {config.background} background."
        )
        if assets:
            instructions += (
                "\n\nIMAGES ARE PROVIDED. Use these exact names and no others: "
                + ", ".join(sorted(assets))
                + ".\n"
                "- Use every one of them, in order, unless the brief says otherwise.\n"
                "- Full bleed: <Img src={staticFile('p0.png')} style={{width: '100%', "
                "height: '100%', objectFit: 'cover'}} />.\n"
                "- Give each one slow movement, a scale from 1.05 to 1.15 across its time on "
                "screen, and cross-fade between them. An image held perfectly still reads as "
                "a broken video.\n"
                "- Put text over a darkened band or beside the image, never across a face."
            )
        if config.narration:
            instructions += (
                "\n\nA VOICE IS ALREADY RECORDED and will play over this video. It is "
                f"{spoken_seconds:g} seconds long. The words, and the second each is spoken:\n"
                + _spoken_outline(words)
                + "\n\nChange scene ON those moments, not on a round number. A cut that lands "
                "on the word being said is the difference between a video and a slideshow "
                "with sound. Leave the bottom fifth of the frame clear: captions go there."
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
                "duration_seconds": duration,
                "max_attempts": config.max_attempts,
                "images": len(assets),
            },
        )

        lines = caption_lines(words) if (config.narration and config.captions) else []
        if config.narration and narration_audio:
            assets = {**assets, "narration.wav": narration_audio}

        def job_for(scene: str) -> RenderJob:
            return RenderJob(
                scene_tsx=scene,
                width=width,
                height=height,
                fps=config.fps,
                duration_seconds=duration,
                props={},
                background=config.background,
                fmt=config.format,
                quality=config.quality,
                assets=assets,
                audio="narration.wav" if (config.narration and narration_audio) else "",
                captions=lines,
            )

        scene = ""
        accepted = False
        wanted_frames = probe_frames(duration, config.fps)
        moments = [frame / config.fps for frame in wanted_frames]

        for attempt in range(1, config.max_attempts + 1):
            await ctx.progress(f"Attempt {attempt}: writing the animation")
            reply = await model.ainvoke(conversation)
            scene = strip_code_fences(message_text_of(reply))

            problems = scene_problems(scene, assets=set(assets) - {"narration.wav"})
            if not problems:
                # Only worth rendering frames from something that at least
                # compiles on paper. A file with no default export produces a
                # bundler error, not a picture.
                try:
                    frames = await probe(job_for(scene), frames=wanted_frames)
                except NodeError as error:
                    problems = [str(error)]
                else:
                    problems = review_frames(frames, seconds=moments)

            await ctx.step(
                "video.attempt",
                {"attempt": attempt, "characters": len(scene), "problems": problems},
            )
            if not problems:
                accepted = True
                break

            await ctx.progress(f"Attempt {attempt} had {len(problems)} problem(s). Revising")
            conversation.append(AIMessage(content=scene))
            conversation.append(
                HumanMessage(
                    content=(
                        "That composition does not work. This is what happened when it was "
                        "rendered:\n"
                        + "\n".join(f"- {problem}" for problem in problems)
                        + "\n\nRewrite the whole composition, fixing these. Reply with ONLY "
                        "the code."
                    )
                )
            )

        if not accepted:
            raise NodeError(
                f"The agent could not produce a working composition in {config.max_attempts} "
                "attempts. The problems from each attempt are on this run. Raising the attempt "
                "limit or simplifying the brief usually helps."
            )

        preview_id = ""
        if config.save_preview:
            # Taken from the accepted composition at full size, which costs one
            # still rather than a second render.
            stills = await probe(
                job_for(scene), frames=[round(duration * config.fps * 0.5)], scale=1.0
            )
            if stills:
                saved_preview = await ctx.save_artifact(
                    stills[0],
                    filename=f"{config.filename}-preview.png",
                    content_type="image/png",
                    node_id=ctx.node_id,
                )
                preview_id = saved_preview["artifact_id"]
                await ctx.step("video.preview", saved_preview)

        narration_id = ""
        if config.narration and narration_audio:
            saved_voice = await ctx.save_artifact(
                narration_audio,
                filename=f"{config.filename}-narration.wav",
                content_type="audio/wav",
                node_id=ctx.node_id,
            )
            narration_id = saved_voice["artifact_id"]
            await ctx.step("video.narration", {**saved_voice, "seconds": spoken_seconds})

        await ctx.progress(f"Rendering {duration:g}s of {config.format}. This takes a while")
        data, info = await render(job_for(scene))
        if not data:
            raise NodeError("The renderer finished but produced no file.")

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
            output={
                **saved,
                "attempts": attempt,
                "duration_seconds": duration,
                "format": config.format,
                "preview_artifact_id": preview_id,
                "narration_artifact_id": narration_id,
                "script": script,
                "words": words,
                "width": info.get("width", width),
                "height": info.get("height", height),
            }
        )


async def _narrate(
    config: Any, ctx: NodeContext, *, model: Any, brief: str, style: str
) -> tuple[str, bytes, float, list[dict[str, Any]]]:
    """Write the script, speak it, and report how long it really took.

    The word budget is given to the agent and then checked, because a model
    told "about seventy words" will cheerfully write ninety. Overrunning is
    not a style problem: the video would end while the voice is still talking.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from basivo_orch.flows.nodes.speech import WORDS_PER_SECOND, speak, word_budget

    budget = word_budget(config.duration_seconds)
    # A floor as well as a ceiling. Asked only for a maximum, a model reliably
    # writes well under it: the first 30-second video came back with 51 words
    # of a 75-word budget and ended with nine seconds of silence.
    floor = max(1, int(budget * 0.85))
    system = NARRATION_INSTRUCTIONS.format(pace=WORDS_PER_SECOND)
    ask = (
        f"{brief}\n\n"
        + (f"Art direction (for tone, not for the words): {style}\n\n" if style else "")
        + f"The video is {config.duration_seconds} seconds, so write between {floor} and "
        f"{budget} words. Use the room: a script well under {floor} words leaves the video "
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
        conversation.append(AIMessageLike(script))
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


def AIMessageLike(text: str) -> Any:  # noqa: N802 - reads as the class it stands in for
    """The model's own reply, put back into the conversation.

    A local import because langchain is only loaded when a node actually calls
    a model, and this file is imported to build the palette on every request.
    """
    from langchain_core.messages import AIMessage

    return AIMessage(content=text)
