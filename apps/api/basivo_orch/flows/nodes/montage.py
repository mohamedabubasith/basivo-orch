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
        from basivo_orch.flows.nodes.remotion import RenderJob, render

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

        props = montage_props(
            names=list(assets),
            plan=plan,
            theme=THEMES[config.theme],
            title=str(plan.get("title") or config.title),
            subtitle=str(plan.get("subtitle") or config.subtitle),
            end_card=config.end_card,
        )

        music_name = ""
        if music_id := config.music_artifact_id.strip():
            music = await ctx.load_artifact(str(render_value(music_id, template)).strip())
            if music is None:
                # A track that has expired costs the soundtrack, not the film.
                await ctx.step("montage.music_missing", {})
            else:
                assets["music.mp3"] = music
                music_name = "music.mp3"

        await ctx.progress(
            f"Rendering {plan['seconds']:g}s from {len(assets)} photographs. This is the slow part."
        )

        data, info = await render(
            RenderJob(
                scene_tsx=MONTAGE_SCENE,
                width=width,
                height=height,
                fps=config.fps,
                duration_seconds=float(plan["seconds"]),
                props=props,
                background=THEMES[config.theme]["bg"],
                fmt="mp4",
                quality=config.quality,
                assets=assets,
                audio=music_name,
            )
        )

        saved = await ctx.save_artifact(
            data, filename="montage.mp4", content_type="video/mp4", node_id=ctx.node_id
        )
        await ctx.step(
            "montage.rendered",
            {
                **saved,
                "seconds": plan["seconds"],
                "photos": len(assets),
                "frames": info.get("durationInFrames"),
            },
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


#: The composition every montage renders through.
#:
#: One component with props rather than generated markup: the shape is the
#: same whether there are three photographs or twelve, and only the list
#: changes. The previous renderer wrote a fresh HTML document per montage,
#: which meant every montage was a composition nobody had ever rendered
#: before.
MONTAGE_SCENE = """import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// Each photograph is oversized and drifts, alternating direction so the film
// does not feel like it is sliding one way for thirty seconds. A photograph
// held perfectly still reads as a broken video, which is the single most
// common complaint about slideshow software.
const DRIFTS = [
  { from: { scale: 1.06, x: -2, y: -1 }, to: { scale: 1.16, x: 2, y: 1 } },
  { from: { scale: 1.14, x: 2, y: 1 }, to: { scale: 1.05, x: -2, y: -1 } },
  { from: { scale: 1.05, x: 0, y: 2 }, to: { scale: 1.15, x: 0, y: -2 } },
  { from: { scale: 1.12, x: -2, y: 1 }, to: { scale: 1.04, x: 2, y: -1 } },
];

export default function Scene({ shots, per, theme, title, subtitle, endCard }) {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();
  const perFrames = Math.max(1, Math.round(per * fps));
  // Long enough to read as a dissolve, short enough that a two second shot is
  // still mostly itself.
  const fade = Math.min(Math.round(fps * 0.5), Math.round(perFrames / 3));
  const unit = Math.min(width, height);

  return (
    <AbsoluteFill style={{ backgroundColor: theme.bg }}>
      {shots.map((shot, index) => {
        const start = index * perFrames;
        const end = start + perFrames;
        // Held one fade beyond its own slot so the next photograph appears
        // underneath rather than against the background.
        if (frame < start - fade || frame > end + fade) return null;

        const drift = DRIFTS[index % DRIFTS.length];
        const progress = interpolate(frame, [start - fade, end + fade], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        });
        const scale = interpolate(progress, [0, 1], [drift.from.scale, drift.to.scale]);
        const x = interpolate(progress, [0, 1], [drift.from.x, drift.to.x]);
        const y = interpolate(progress, [0, 1], [drift.from.y, drift.to.y]);
        const opacity = interpolate(
          frame,
          [start - fade, start, end, end + fade],
          [0, 1, 1, 0],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
        );

        return (
          <AbsoluteFill key={shot.name} style={{ opacity }}>
            <Img
              src={staticFile(shot.name)}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "cover",
                transform: `scale(${scale}) translate(${x}%, ${y}%)`,
              }}
            />
            {shot.caption ? (
              <AbsoluteFill
                style={{
                  justifyContent: "flex-end",
                  alignItems: "center",
                  paddingBottom: unit * 0.12,
                  // A gradient rather than a band: a hard edge across a
                  // photograph looks like a mistake, and text with nothing
                  // behind it disappears over a bright sky.
                  background:
                    "linear-gradient(to top, rgba(0,0,0,0.72) 0%, rgba(0,0,0,0) 38%)",
                }}
              >
                <div
                  style={{
                    color: theme.ink,
                    fontFamily: theme.font,
                    fontSize: unit * 0.052,
                    textAlign: "center",
                    maxWidth: "82%",
                    textShadow: "0 2px 10px rgba(0,0,0,0.6)",
                  }}
                >
                  {shot.caption}
                </div>
              </AbsoluteFill>
            ) : null}
          </AbsoluteFill>
        );
      })}

      {title ? (
        <AbsoluteFill
          style={{
            justifyContent: "center",
            alignItems: "center",
            textAlign: "center",
            opacity: interpolate(frame, [0, fps * 0.4, fps * 2.2, fps * 2.8], [0, 1, 1, 0], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
            background: "rgba(0,0,0,0.35)",
          }}
        >
          <div>
            <div
              style={{
                color: theme.ink,
                fontFamily: theme.font,
                fontSize: unit * 0.095,
                letterSpacing: unit * 0.002,
              }}
            >
              {title}
            </div>
            {subtitle ? (
              <div
                style={{
                  color: theme.accent,
                  fontFamily: theme.font,
                  fontSize: unit * 0.042,
                  marginTop: unit * 0.02,
                }}
              >
                {subtitle}
              </div>
            ) : null}
          </div>
        </AbsoluteFill>
      ) : null}

      {endCard ? (
        <AbsoluteFill
          style={{
            justifyContent: "center",
            alignItems: "center",
            textAlign: "center",
            backgroundColor: theme.bg,
            opacity: interpolate(
              frame,
              [durationInFrames - fps * 1.6, durationInFrames - fps * 1.2],
              [0, 1],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
            ),
          }}
        >
          <div
            style={{
              color: theme.accent,
              fontFamily: theme.font,
              fontSize: unit * 0.07,
              maxWidth: "80%",
            }}
          >
            {endCard}
          </div>
        </AbsoluteFill>
      ) : null}
    </AbsoluteFill>
  );
}
"""


def montage_props(
    *,
    names: list[str],
    plan: dict[str, Any],
    theme: dict[str, str],
    title: str,
    subtitle: str,
    end_card: str,
) -> dict[str, Any]:
    """What the composition is handed: the shots, in order, with their words."""
    order = plan["order"]
    shots = [
        {
            "name": name,
            "caption": plan["captions"].get(order[index] if index < len(order) else "", ""),
        }
        for index, name in enumerate(names)
    ]
    return {
        "shots": shots,
        "per": float(plan["per_photo"]),
        "theme": theme,
        "title": title,
        "subtitle": subtitle,
        "endCard": end_card,
    }
