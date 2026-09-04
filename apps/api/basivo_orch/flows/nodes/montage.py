"""Photographs into a film.

The deliberate choice, and the one worth defending: this composes a montage
rather than asking a video model to invent motion. A generated clip costs money
per second, cannot be reproduced twice the same way, and does unpredictable
things to faces — which is unusable when the faces belong to a couple on their
wedding day and the studio's name is on the result. Panning across a photograph
the studio actually took is free, identical every time, and honest.

What makes it look like a film rather than a slideshow is entirely in the
timing: each photograph drifts slowly (a Ken Burns move), consecutive ones
cross-fade rather than cut, the first frame holds a title, and the whole thing
lands on the beat of a fade to the studio's name. None of that needs a model.

A model *is* useful for one thing, and only one: deciding the order, the
captions, and which photograph deserves the title card. That arrives here as a
plan, and when it is absent this makes a perfectly good default one — so the
bot still works on a day when the model provider does not.
"""

from __future__ import annotations

import html as html_escape
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

#: Below this a montage is a flash of images; above it, on a two-core box, the
#: render outlives the operator's patience.
MIN_SECONDS, MAX_SECONDS = 5.0, 60.0
#: A photograph needs this long on screen to be looked at rather than noticed.
MIN_PHOTO_SECONDS = 1.6
#: Overlap between one photograph and the next.
CROSSFADE_SECONDS = 0.7

THEMES: dict[str, dict[str, str]] = {
    # Warm ivory and gold: the palette of most Indian wedding albums.
    "classic": {"bg": "#120d0a", "ink": "#fdf6ec", "accent": "#d4af37", "font": "Georgia, serif"},
    "modern": {"bg": "#0b0b12", "ink": "#ffffff", "accent": "#8b7cf6", "font": "Inter, sans-serif"},
    "film": {"bg": "#0a0a0a", "ink": "#f5f5f0", "accent": "#c8a882", "font": "Georgia, serif"},
    "blush": {"bg": "#1a0f14", "ink": "#fff5f7", "accent": "#e8a0b4", "font": "Georgia, serif"},
}

ASPECTS: dict[str, tuple[int, int]] = {
    # Portrait first: this is delivered on a phone and forwarded on WhatsApp.
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


class MontageConfig(BaseModel):
    model_config = {"extra": "forbid"}

    #: Usually {{ input.photo_ids }} from the conversation state.
    photos: str = Field(default="", title="Photos", description="Artifact ids, or a reference.")
    seconds: float = Field(default=20.0, ge=MIN_SECONDS, le=MAX_SECONDS, title="Length")
    aspect: Literal["9:16", "16:9", "1:1", "4:5"] = Field(default="9:16", title="Shape")
    theme: Literal["classic", "modern", "film", "blush"] = Field(default="classic", title="Look")
    title: str = Field(default="", max_length=80, title="Title")
    subtitle: str = Field(default="", max_length=120, title="Subtitle")
    end_card: str = Field(default="", max_length=80, title="Closing line")
    #: A JSON plan from an upstream agent: order, captions, which photo leads.
    #: Optional by design — see the module docstring.
    plan: str = Field(default="", max_length=20_000, title="Director's plan")
    music_artifact_id: str = Field(default="", title="Music")
    fps: int = Field(default=30, ge=12, le=60, title="Frames per second")
    quality: Literal["draft", "standard", "high"] = Field(default="standard", title="Quality")

    @field_validator("photos")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return value.strip()


class MontageNode(Node):
    type = "video.montage"
    label = "Photo Montage"
    description = "Turn photographs into a video with motion, music and titles."
    when = (
        "You have a set of photos and want a short film from them with no generative model "
        "involved: predictable, fast, cheap."
    )
    needs = ("Photos from the trigger or Prepare Photo.",)
    example = "Telegram Bot -> Prepare Photo -> Photo Montage -> Telegram Reply"
    tier = 3
    category = "design"
    config_model = MontageConfig
    output_paths = ("artifact_id", "url", "seconds", "photo_count", "width", "height")

    async def run(self, config: MontageConfig, ctx: NodeContext) -> NodeResult:
        from basivo_orch.flows.nodes.video import VideoRenderConfig, _render

        if ctx.load_artifact is None or ctx.save_artifact is None:
            raise NodeError("A montage can only be made inside a real run.")

        template = ctx.template_context()
        ids = _photo_ids(render_value(config.photos, template))
        if not ids:
            raise NodeError(
                "No photographs to work with. Send some to the bot first, or point "
                "this at {{ input.photo_ids }}."
            )

        plan = _plan(config, ids)
        width, height = ASPECTS[config.aspect]

        # Photographs become files beside index.html rather than base64 inside
        # it: twelve pictures inline is several megabytes of HTML for Chromium
        # to parse before it draws a single frame.
        assets: dict[str, bytes] = {}
        for index, artifact_id in enumerate(plan["order"]):
            data = await ctx.load_artifact(artifact_id)
            if data is None:
                # One missing photograph should cost that photograph, not the
                # job — artifacts expire, and a job iterated on for two days
                # will find one gone.
                await ctx.step("montage.photo_missing", {"artifact_id": artifact_id})
                continue
            assets[f"p{index}.jpg"] = data
        if not assets:
            raise NodeError(
                "None of those photographs are still stored. Send them again and "
                "the video can be remade."
            )

        composition = build_composition(
            names=list(assets),
            plan=plan,
            width=width,
            height=height,
            fps=config.fps,
            theme=THEMES[config.theme],
            music=bool(config.music_artifact_id.strip()),
        )

        if music_id := config.music_artifact_id.strip():
            music = await ctx.load_artifact(str(render_value(music_id, template)).strip())
            if music is None:
                await ctx.step("montage.music_missing", {})
            else:
                assets["music.mp3"] = music

        await ctx.progress(
            f"Rendering {plan['seconds']:g}s from {len(assets)} photographs. This is the slow part."
        )

        render_config = VideoRenderConfig(
            template="custom",
            html=composition,
            format="mp4",
            fps=config.fps,
            quality=config.quality,
            filename="montage",
        )
        data, logs = await _render(composition, variables={}, config=render_config, assets=assets)

        saved = await ctx.save_artifact(
            data, filename="montage.mp4", content_type="video/mp4", node_id=ctx.node_id
        )
        await ctx.step(
            "montage.rendered",
            {**saved, "seconds": plan["seconds"], "photos": len(assets), "log_tail": logs[-400:]},
        )
        return NodeResult(
            output={
                **saved,
                "seconds": plan["seconds"],
                "photo_count": len(assets),
                "width": width,
                "height": height,
            }
        )


def _photo_ids(value: Any) -> list[str]:
    """Accept the several shapes a flow might hand over.

    A reference resolves to a real list; a person typing into the field writes
    commas; an agent writes JSON. All three are the same intent.
    """
    if isinstance(value, list):
        candidates = [str(item) for item in value]
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                candidates = [str(item) for item in json.loads(text)]
            except ValueError:
                candidates = []
        else:
            candidates = [part.strip() for part in text.replace("\n", ",").split(",")]
    else:
        candidates = []

    kept: list[str] = []
    for candidate in candidates:
        try:
            kept.append(str(uuid.UUID(candidate.strip())))
        except (ValueError, AttributeError):
            continue
    return kept


def _plan(config: MontageConfig, ids: list[str]) -> dict[str, Any]:
    """The edit: which photograph, for how long, with what caption.

    A model's plan is used when it is valid and ignored when it is not. The
    fallback is not a degraded mode — an even cut across the photographs, with
    the title on the first, is what most of these should be anyway.
    """
    order = list(ids)
    captions: dict[str, str] = {}
    title, subtitle = config.title, config.subtitle

    if config.plan.strip():
        try:
            given = json.loads(config.plan)
            if isinstance(given, dict):
                # Only ids we actually hold, in the order the model asked for,
                # with anything it forgot appended rather than dropped.
                asked = [str(item) for item in given.get("order", []) if str(item) in set(ids)]
                order = asked + [item for item in ids if item not in set(asked)]
                captions = {str(k): str(v)[:90] for k, v in (given.get("captions") or {}).items()}
                title = str(given.get("title") or title)[:80]
                subtitle = str(given.get("subtitle") or subtitle)[:120]
        except ValueError:
            # A model that returned prose instead of JSON does not get to stop
            # the job.
            pass

    # Fit the photographs to the length asked for, not the other way round: a
    # 20 second video with 30 photographs is a flicker book, so it keeps the
    # ones it can show properly and says how many.
    usable = max(1, min(len(order), int(config.seconds // MIN_PHOTO_SECONDS)))
    order = order[:usable]
    per_photo = round(config.seconds / len(order), 3)

    return {
        "order": order,
        "captions": captions,
        "title": title,
        "subtitle": subtitle,
        "end_card": config.end_card,
        "per_photo": per_photo,
        "seconds": round(per_photo * len(order), 3),
        "dropped": max(0, len(ids) - len(order)),
    }


def build_composition(
    *,
    names: list[str],
    plan: dict[str, Any],
    width: int,
    height: int,
    fps: int,
    theme: dict[str, str],
    music: bool,
) -> str:
    """The HyperFrames composition, written out.

    Generated rather than templated with variables, because the structure
    itself depends on the number of photographs — the existing templates vary
    their text, this varies its shape.

    The motion is the whole trick. Each photograph is oversized and drifts
    across the frame, alternating direction so the film does not feel like it
    is sliding one way for thirty seconds; the drift is slow enough to read as
    intentional rather than as a zoom.
    """
    per = float(plan["per_photo"])
    order = plan["order"]
    layers, timeline = [], []

    for index, name in enumerate(names):
        start = round(index * per, 3)
        artifact_id = order[index] if index < len(order) else ""
        caption = plan["captions"].get(artifact_id, "")
        # Alternating so consecutive shots move differently.
        drift = [
            ("scale(1.06) translate(-2%, -1%)", "scale(1.16) translate(2%, 1%)"),
            ("scale(1.14) translate(2%, 1%)", "scale(1.05) translate(-2%, -1%)"),
            ("scale(1.05) translate(0, 2%)", "scale(1.15) translate(0, -2%)"),
        ][index % 3]

        layers.append(
            f'<div class="shot" id="s{index}" style="opacity:0">'
            f'<img src="{name}" alt="">'
            + (f'<div class="caption">{html_escape.escape(caption)}</div>' if caption else "")
            + "</div>"
        )
        timeline.append(
            f"tl.fromTo('#s{index} img', {{transform:'{drift[0]}'}}, "
            f"{{transform:'{drift[1]}', duration:{per + CROSSFADE_SECONDS:.3f}, "
            f"ease:'none'}}, {start:.3f});"
        )
        # The first shot fades up from the title card; the rest cross-fade.
        fade_in = CROSSFADE_SECONDS if index else 0.9
        timeline.append(
            f"tl.to('#s{index}', {{opacity:1, duration:{fade_in:.3f}, "
            f"ease:'power1.out'}}, {max(0.0, start - CROSSFADE_SECONDS / 2):.3f});"
        )
        if index < len(names) - 1:
            timeline.append(
                f"tl.to('#s{index}', {{opacity:0, duration:{CROSSFADE_SECONDS:.3f}, "
                f"ease:'power1.in'}}, {start + per - CROSSFADE_SECONDS / 2:.3f});"
            )
        if caption:
            timeline.append(
                f"tl.fromTo('#s{index} .caption', {{y:24, opacity:0}}, "
                f"{{y:0, opacity:1, duration:0.6, ease:'power2.out'}}, {start + 0.35:.3f});"
            )

    total = round(per * len(names), 3)
    title_block = ""
    if plan["title"] or plan["subtitle"]:
        title_block = (
            '<div class="title" id="title">'
            f'<div class="t">{html_escape.escape(plan["title"])}</div>'
            f'<div class="s">{html_escape.escape(plan["subtitle"])}</div>'
            "</div>"
        )
        timeline.append(
            "tl.fromTo('#title', {opacity:0, y:18}, "
            "{opacity:1, y:0, duration:1.0, ease:'power2.out'}, 0.25);"
        )
        timeline.append("tl.to('#title', {opacity:0, duration:0.7, ease:'power1.in'}, 3.2);")

    end_block = ""
    if plan["end_card"]:
        end_block = f'<div class="end" id="end">{html_escape.escape(plan["end_card"])}</div>'
        timeline.append(
            "tl.fromTo('#end', {opacity:0}, {opacity:1, duration:0.8, ease:'power1.out'}, "
            f"{max(0.0, total - 2.0):.3f});"
        )

    audio = (
        '<audio data-audio-src="music.mp3" data-audio-volume="0.8" '
        f'data-audio-fade-out="1.5" data-audio-duration="{total:.3f}"></audio>'
        if music
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin:0; background:{theme["bg"]}; }}
  #stage {{ position:relative; width:{width}px; height:{height}px; overflow:hidden;
           background:{theme["bg"]}; color:{theme["ink"]};
           font-family:{theme["font"]}; }}
  .shot {{ position:absolute; inset:0; }}
  .shot img {{ width:100%; height:100%; object-fit:cover; will-change:transform; }}
  /* A gradient at the foot of every frame, so a caption stays readable over a
     bright photograph without a slab of colour behind it. */
  .shot::after {{ content:''; position:absolute; inset:0;
    background:linear-gradient(to top, {theme["bg"]}cc 0%, transparent 38%); }}
  .caption {{ position:absolute; left:6%; right:6%; bottom:9%; z-index:2;
    font-size:{int(height * 0.031)}px; line-height:1.35; text-align:center;
    text-shadow:0 2px 18px rgba(0,0,0,.55); }}
  .title {{ position:absolute; inset:0; z-index:3; display:flex; flex-direction:column;
    align-items:center; justify-content:center; text-align:center;
    background:linear-gradient(180deg, {theme["bg"]}99, {theme["bg"]}dd); }}
  .title .t {{ font-size:{int(height * 0.058)}px; letter-spacing:.02em; }}
  .title .s {{ margin-top:{int(height * 0.018)}px; font-size:{int(height * 0.026)}px;
    color:{theme["accent"]}; letter-spacing:.16em; text-transform:uppercase; }}
  .end {{ position:absolute; inset:0; z-index:4; display:flex; align-items:center;
    justify-content:center; background:{theme["bg"]}; color:{theme["accent"]};
    font-size:{int(height * 0.032)}px; letter-spacing:.14em; opacity:0; }}
</style></head>
<body>
<div id="stage" data-composition-id="montage" data-start="0" data-duration="{total:.3f}"
     data-width="{width}" data-height="{height}" data-fps="{fps}"
     data-composition-variables='{{}}'>
  <div class="clip layer" data-start="0" data-duration="{total:.3f}" data-track-index="0">
    {"".join(layers)}
    {title_block}
    {end_block}
    {audio}
  </div>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    {" ".join(timeline)}
    window.__timelines = window.__timelines || {{}};
    window.__timelines['montage'] = tl;
  </script>
</div>
</body></html>"""
