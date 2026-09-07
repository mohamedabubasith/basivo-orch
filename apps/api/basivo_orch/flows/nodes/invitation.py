"""Wedding invitation films.

What a studio is actually asked for. Not a montage of an event that happened —
a film sent to guests before it, carrying the same information a printed card
carries: whose wedding, when, where, which function is on which day, and who is
inviting you. It is read on a phone, forwarded on WhatsApp, and watched with
the sound off, which decides almost every choice below.

Three of those choices are worth stating.

**The ornament is drawn, not photographed.** The mandala, the divider and the
corners are inline SVG inside the component, so there is no asset server and no
image that renders at a different size on a different machine. Only the
couple's own photographs arrive as files.

**The type is the design.** An invitation is mostly words, held still long
enough to be read: names large, everything else quiet around them. The motion
exists to bring a line in and let it settle, never to be noticed.

**Watched with the sound off.** Every fact is on screen as text. Music, when
there is any, is decoration rather than delivery, and it is the render job's
audio track rather than anything the composition knows about.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

MIN_SECONDS, MAX_SECONDS = 8.0, 60.0

#: An invitation stack is set in a serif. EB Garamond is installed in the
#: worker image; the Noto families carry Tamil and Devanagari so a name in
#: either does not render as boxes. Order matters: the first family that has a
#: glyph wins, per character, so a card with an English line and a Tamil name
#: gets Garamond for one and Noto Serif Tamil for the other.
SERIF = '"EB Garamond", "Noto Serif", "Noto Serif Devanagari", "Noto Serif Tamil", Georgia, serif'

PALETTES: dict[str, dict[str, str]] = {
    # The traditional South Indian wedding card: deep maroon, gold, ivory.
    "maroon_gold": {"bg": "#3b0d17", "deep": "#2a0910", "ink": "#fbf1e3", "gold": "#e0b768"},
    # Ivory with gold, for a daytime or Christian ceremony.
    "ivory_gold": {"bg": "#f7f1e6", "deep": "#efe6d6", "ink": "#2e2013", "gold": "#b0873a"},
    "emerald_gold": {"bg": "#0f2a22", "deep": "#0a1e18", "ink": "#f3f7f2", "gold": "#d8bf7a"},
    "blush_rose": {"bg": "#2a1119", "deep": "#1e0c12", "ink": "#fdf0f2", "gold": "#e5a3ac"},
    "royal_blue": {"bg": "#101a3a", "deep": "#0a1029", "ink": "#f2f4ff", "gold": "#d9c07c"},
}

ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
}


class Function(BaseModel):
    """One event in the schedule: Mehendi, Haldi, Sangeet, the wedding itself."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=40)
    when: str = Field(default="", max_length=60)
    where: str = Field(default="", max_length=80)


class InvitationConfig(BaseModel):
    model_config = {"extra": "forbid"}

    #: One or two photographs of the couple. An invitation with none still
    #: works — plenty of families prefer only the ornament.
    photos: str = Field(default="", title="Photos", description="Artifact ids, or a reference.")

    invite_line: str = Field(
        default="Together with their families",
        max_length=90,
        title="Opening line",
    )
    bride: str = Field(default="", max_length=40, title="Bride")
    groom: str = Field(default="", max_length=40, title="Groom")
    joiner: str = Field(default="&", max_length=12, title="Between the names")
    date_line: str = Field(default="", max_length=60, title="Date")
    time_line: str = Field(default="", max_length=60, title="Time")
    venue: str = Field(default="", max_length=120, title="Venue")
    functions: list[Function] = Field(default_factory=list, max_length=6, title="Functions")
    closing: str = Field(
        default="", max_length=90, title="Closing line", description="e.g. With love, the families"
    )
    #: A blessing or motif above the names. Empty by default: it is a religious
    #: choice, and defaulting to one imposes it on every customer.
    header_symbol: str = Field(default="", max_length=40, title="Blessing")

    #: Everything above, as one JSON object, for when an agent upstream reads
    #: the operator's message and fills the card in. Same arrangement as the
    #: montage's plan and for the same reason: a typed list cannot be a
    #: template reference, and "Mehendi on the 10th, Sangeet on the 11th" is
    #: exactly the sort of thing a person types and a model is good at
    #: structuring.
    details: str = Field(default="", max_length=8_000, title="Details as JSON")

    seconds: float = Field(default=22.0, ge=MIN_SECONDS, le=MAX_SECONDS, title="Length")
    aspect: Literal["9:16", "1:1", "4:5", "16:9"] = Field(default="9:16", title="Shape")
    palette: Literal["maroon_gold", "ivory_gold", "emerald_gold", "blush_rose", "royal_blue"] = (
        Field(default="maroon_gold", title="Colours")
    )
    music_artifact_id: str = Field(default="", title="Music")
    fps: int = Field(default=30, ge=12, le=60, title="Frames per second")
    quality: Literal["draft", "standard", "high"] = Field(default="standard", title="Quality")

    @field_validator("bride", "groom", "venue", "date_line")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return value.strip()


class InvitationNode(Node):
    type = "video.invitation"
    label = "Wedding Invitation"
    description = "An invitation film with names, date, venue and schedule."
    when = (
        "A wedding or ceremony invitation: the details are known, the couple's photos are in "
        "hand, and the studio wants the finished film."
    )
    needs = (
        "The event details, typed or filled in by an AI Agent from a message.",
        "Photos from the trigger or Prepare Photo.",
    )
    example = "Telegram Bot -> AI Agent -> Wedding Invitation -> Telegram Reply"
    tier = 3
    category = "design"
    config_model = InvitationConfig
    output_paths = ("artifact_id", "url", "seconds", "width", "height")

    async def run(self, config: InvitationConfig, ctx: NodeContext) -> NodeResult:
        from basivo_orch.flows.nodes.montage import _photo_ids
        from basivo_orch.flows.nodes.remotion import RenderJob, render

        if ctx.save_artifact is None:
            raise NodeError("An invitation can only be made inside a real run.")

        template = ctx.template_context()
        if config.details.strip():
            config = merge_details(config, str(render_value(config.details, template)))

        if not (config.bride or config.groom):
            raise NodeError("An invitation needs at least one name on it. Set the bride and groom.")

        ids = _photo_ids(render_value(config.photos, template)) if config.photos else []

        assets: dict[str, bytes] = {}
        for index, artifact_id in enumerate(ids[:2]):
            if ctx.load_artifact and (data := await ctx.load_artifact(artifact_id)):
                assets[f"couple{index}.jpg"] = data

        width, height = ASPECTS[config.aspect]
        palette = PALETTES[config.palette]
        # The photographs, and only those: the music is added to the same
        # dictionary below, and the composition must never be told about it.
        props = invitation_props(config=config, photos=list(assets), palette=palette)

        music_name = ""
        if music_id := str(render_value(config.music_artifact_id, template)).strip():
            if ctx.load_artifact and (music := await ctx.load_artifact(music_id)):
                assets["music.mp3"] = music
                music_name = "music.mp3"

        await ctx.progress(f"Rendering a {config.seconds:g}s invitation")

        data, info = await render(
            RenderJob(
                scene_tsx=INVITATION_SCENE,
                width=width,
                height=height,
                fps=config.fps,
                duration_seconds=float(config.seconds),
                props=props,
                background=palette["bg"],
                fmt="mp4",
                quality=config.quality,
                assets=assets,
                audio=music_name,
            )
        )

        saved = await ctx.save_artifact(
            data, filename="invitation.mp4", content_type="video/mp4", node_id=ctx.node_id
        )
        await ctx.step(
            "invitation.rendered",
            {
                **saved,
                "seconds": config.seconds,
                "photos": len(props["photos"]),
                "frames": info.get("durationInFrames"),
            },
        )
        return NodeResult(
            output={**saved, "seconds": config.seconds, "width": width, "height": height}
        )


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------

#: The component every invitation renders through.
#:
#: One component with props rather than markup generated per card: the shape is
#: the same whether the couple sent two photographs or none, and only the props
#: change. Everything it needs to know about size comes from useVideoConfig and
#: everything it needs to know about time comes from durationInFrames, so the
#: same file is the film at 9:16 and at 16:9, at twelve seconds and at forty.
INVITATION_SCENE = """import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const CLAMP = { extrapolateLeft: "clamp", extrapolateRight: "clamp" };

// Scene lengths as fractions of the whole, so a twelve second cut and a thirty
// second cut are the same film at different paces rather than the same scenes
// with a longer gap at the end. The proportions come from what a reader needs:
// a name is taken in at a glance, a schedule of five functions is not.
const WEIGHTS = { open: 0.18, names: 0.3, when: 0.2, functions: 0.22, close: 0.12 };
const ORDER = ["open", "names", "when", "functions", "close"];

// Where the couple's photograph drifts from and to: scale, then a vertical
// nudge in percent. The second one moves the other way so two photographs do
// not both slide the same direction.
const DRIFTS = [
  [1.04, 1.14, 1, -1],
  [1.12, 1.03, -1, 1],
];

function marksFor(total, hasFunctions) {
  const weights = hasFunctions ? WEIGHTS : { ...WEIGHTS, functions: 0, close: 0.32 };
  const marks = {};
  let cursor = 0;
  for (const key of ORDER) {
    const length = Math.round(total * weights[key]);
    marks[key] = [cursor, length];
    cursor += length;
  }
  // Rounding leaves a few frames over. The closing card holds them, rather
  // than the film ending on a blank frame.
  marks.close[1] += total - cursor;
  return marks;
}

// Every element on screen goes through this: it comes in, settles, and leaves.
// One function rather than a rule per element, because the bug this design
// keeps having is something hidden by one thing and revealed by another.
function enter(frame, mark, fps, rise = 0, hold = false) {
  const [start, length] = mark;
  const ramp = Math.min(length * 0.3, fps * 0.9);
  const leave = Math.min(length * 0.25, fps * 0.55);
  const stops = hold
    ? [start, start + ramp]
    : [start, start + ramp, start + length - leave, start + length];
  const values = hold ? [0, 1] : [0, 1, 1, 0];
  return {
    opacity: interpolate(frame, stops, values, CLAMP),
    transform: `translateY(${interpolate(frame, [start, start + ramp], [rise, 0], CLAMP)}px)`,
  };
}

function Photo({ name, mark, frame, fps, palette }) {
  const [start, length] = mark;
  if (frame < start || frame > start + length) return null;

  const [fromScale, toScale, fromShift, toShift] = DRIFTS[name.endsWith("1.jpg") ? 1 : 0];
  const progress = interpolate(frame, [start, start + length], [0, 1], CLAMP);
  const scale = interpolate(progress, [0, 1], [fromScale, toScale]);
  const shift = interpolate(progress, [0, 1], [fromShift, toShift]);
  const fade = Math.min(fps * 0.8, length / 3);

  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: 0,
        // The photograph takes the upper two thirds and the words sit below
        // it. A photography studio is selling the photograph, so an
        // invitation that hides the couple behind full frame type is worse
        // than one with no picture at all.
        height: "64%",
        opacity: interpolate(
          frame,
          [start, start + fade, start + length - fade, start + length],
          [0, 1, 1, 0],
          CLAMP,
        ),
      }}
    >
      <Img
        src={staticFile(name)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          objectPosition: "center 32%",
          transform: `scale(${scale}) translateY(${shift}%)`,
        }}
      />
      {/* Only the edges are darkened: enough to seat the picture in the page
          and carry it into the background, never across the faces. */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `linear-gradient(180deg, ${palette.deep}88 0%, transparent 20%,
            transparent 46%, ${palette.deep}55 68%, ${palette.deep}cc 86%, ${palette.bg} 100%)`,
        }}
      />
    </div>
  );
}

// The ring behind the opening line. Arcs and petals rather than an image: it
// scales to any frame, weighs nothing, and can turn while it appears, which is
// what gives an invitation its first two seconds.
function Mandala({ size, colour, spin }) {
  const petals = [];
  for (let angle = 0; angle < 360; angle += 30) {
    petals.push(
      <path
        key={angle}
        d="M50 14 C 58 26, 58 34, 50 44 C 42 34, 42 26, 50 14 Z"
        transform={`rotate(${angle} 50 50)`}
        fill="none"
        stroke={colour}
        strokeWidth="1.2"
        opacity="0.9"
      />,
    );
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      style={{ transform: `rotate(${spin}deg)` }}
    >
      <circle cx="50" cy="50" r="46" fill="none" stroke={colour} strokeWidth="1.1"
        strokeDasharray="1.8 2.6" opacity="0.85" />
      <circle cx="50" cy="50" r="39" fill="none" stroke={colour} strokeWidth="1.6" />
      <circle cx="50" cy="50" r="30" fill="none" stroke={colour} strokeWidth="0.9" opacity="0.75" />
      {petals}
    </svg>
  );
}

// A rule with a diamond in the middle. The full stop of a wedding card.
function Divider({ width, colour }) {
  return (
    <svg width={width} height={width * 0.06} viewBox="0 0 200 12">
      <line x1="10" y1="6" x2="86" y2="6" stroke={colour} strokeWidth="0.8" />
      <path d="M100 1 L106 6 L100 11 L94 6 Z" fill={colour} />
      <line x1="114" y1="6" x2="190" y2="6" stroke={colour} strokeWidth="0.8" />
    </svg>
  );
}

// Floral corners, which is what makes a plain frame read as a card. They draw
// themselves in on the closing card, one after another.
function Corners({ unit, colour, frame, mark, fps }) {
  const [start] = mark;
  const places = [
    { top: unit * 4, left: unit * 4, flip: "" },
    { top: unit * 4, right: unit * 4, flip: "scaleX(-1)" },
    { bottom: unit * 4, left: unit * 4, flip: "scaleY(-1)" },
    { bottom: unit * 4, right: unit * 4, flip: "scale(-1,-1)" },
  ];
  return (
    <>
      {places.map((place, index) => {
        const from = start + fps * 0.2 + index * fps * 0.12;
        const grow = interpolate(frame, [from, from + fps * 0.8], [0.8, 1], CLAMP);
        const { flip, ...position } = place;
        return (
          <div
            key={index}
            style={{
              position: "absolute",
              width: unit * 11,
              height: unit * 11,
              opacity: interpolate(frame, [from, from + fps * 0.8], [0, 1], CLAMP),
              transform: `${flip} scale(${grow})`,
              ...position,
            }}
          >
            <svg width="100%" height="100%" viewBox="0 0 100 100">
              <path d="M4 40 C 4 16, 16 4, 40 4" fill="none" stroke={colour} strokeWidth="1.4" />
              <path d="M4 62 C 4 26, 26 4, 62 4" fill="none" stroke={colour} strokeWidth="0.7"
                opacity="0.65" />
              <circle cx="12" cy="12" r="2.4" fill={colour} />
            </svg>
          </div>
        );
      })}
    </>
  );
}

export default function Scene({
  photos,
  palette,
  font,
  symbol,
  inviteLine,
  bride,
  groom,
  joiner,
  dateLine,
  timeLine,
  venue,
  functions,
  closing,
}) {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, width, height } = useVideoConfig();

  // Type is sized from the frame rather than from a fixed number, so all four
  // shapes are the same design. The height sets the scale and the width caps
  // it, which is what keeps a long name inside a 16:9 frame and stops a
  // portrait one from being set in small type.
  const unit = Math.min(height, width * 1.4) / 100;
  const marks = marksFor(durationInFrames, functions.length > 0);
  const overlaid = photos.length > 0;

  // The whole card breathes for the length of the film. Each scene moves on
  // its own too, but this is what guarantees no frame is a copy of the one
  // before it, which is the difference between a video and a slideshow that
  // took minutes to encode.
  const breath = interpolate(frame, [0, durationInFrames], [1, 1.035], CLAMP);

  const text = { color: palette.ink, fontFamily: font, textAlign: "center" };
  const card = {
    justifyContent: overlaid ? "flex-end" : "center",
    alignItems: "center",
    padding: `0 ${unit * 9}px`,
    paddingBottom: overlaid ? unit * 11 : 0,
  };
  const middle = { justifyContent: "center", alignItems: "center", padding: `0 ${unit * 9}px` };

  const namesRule = interpolate(
    frame,
    [marks.names[0] + fps * 0.75, marks.names[0] + fps * 1.65],
    [0, 1],
    CLAMP,
  );

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(120% 90% at 50% 20%, ${palette.bg} 0%, ${palette.deep} 100%)`,
      }}
    >
      <AbsoluteFill style={{ transform: `scale(${breath})` }}>
        {photos.map((name, index) => (
          <Photo
            key={name}
            name={name}
            frame={frame}
            fps={fps}
            palette={palette}
            mark={
              index === 0
                ? [marks.names[0] - fps * 0.5, marks.names[1] + fps * 0.5]
                : [marks.when[0] - fps * 0.4, marks.when[1] + fps * 0.4]
            }
          />
        ))}

        <AbsoluteFill style={{ ...middle, ...enter(frame, marks.open, fps, unit * 1.4) }}>
          {symbol ? (
            <div style={{ ...text, color: palette.gold, fontSize: unit * 3.4,
              marginBottom: unit * 1.6 }}>
              {symbol}
            </div>
          ) : null}
          <Mandala
            size={unit * 34}
            colour={palette.gold}
            spin={interpolate(
              frame,
              [marks.open[0], marks.open[0] + marks.open[1]],
              [-8, 6],
              CLAMP,
            )}
          />
          <div
            style={{
              ...text,
              color: palette.gold,
              fontSize: unit * 2.5,
              marginTop: unit * 2.4,
              letterSpacing: "0.2em",
              textTransform: "uppercase",
            }}
          >
            {inviteLine}
          </div>
        </AbsoluteFill>

        <AbsoluteFill style={{ ...card, ...enter(frame, marks.names, fps, unit * 2.2) }}>
          {[bride, groom].map((name, index) =>
            name ? (
              <React.Fragment key={index}>
                {index === 1 && bride && joiner ? (
                  <div style={{ ...text, color: palette.gold, fontSize: unit * 3.6,
                    margin: `${unit * 0.8}px 0` }}>
                    {joiner}
                  </div>
                ) : null}
                <div
                  style={{
                    ...text,
                    fontSize: unit * 7.2,
                    lineHeight: 1.06,
                    maxWidth: "100%",
                    overflowWrap: "break-word",
                  }}
                >
                  {name}
                </div>
              </React.Fragment>
            ) : null,
          )}
          <div
            style={{
              width: unit * 22 * namesRule,
              height: 2,
              backgroundColor: palette.gold,
              marginTop: unit * 2.2,
            }}
          />
        </AbsoluteFill>

        <AbsoluteFill style={{ ...card, ...enter(frame, marks.when, fps, unit * 1.6) }}>
          <div style={{ ...text, fontSize: unit * 4, letterSpacing: "0.03em" }}>{dateLine}</div>
          {timeLine ? (
            <div
              style={{
                ...text,
                color: palette.gold,
                fontSize: unit * 2.4,
                marginTop: unit,
                letterSpacing: "0.18em",
                textTransform: "uppercase",
              }}
            >
              {timeLine}
            </div>
          ) : null}
          <div style={{ margin: `${unit * 1.8}px 0` }}>
            <Divider width={unit * 24} colour={palette.gold} />
          </div>
          {venue ? (
            <div style={{ ...text, fontSize: unit * 2.3, lineHeight: 1.45, opacity: 0.92,
              whiteSpace: "pre-line" }}>
              {venue}
            </div>
          ) : null}
        </AbsoluteFill>

        {functions.length > 0 ? (
          <AbsoluteFill style={{ ...middle, ...enter(frame, marks.functions, fps) }}>
            <div
              style={{
                ...text,
                color: palette.gold,
                fontSize: unit * 2.2,
                letterSpacing: "0.22em",
                textTransform: "uppercase",
                marginBottom: unit * 2.4,
              }}
            >
              Celebrations
            </div>
            {functions.map((item, index) => {
              // Staggered, because a schedule that appears all at once is a
              // wall. The step shrinks as the list grows so the last row is
              // still on screen for a while.
              const step = Math.min(fps * 0.5, (marks.functions[1] - fps * 1.4) / functions.length);
              const from = marks.functions[0] + fps * 0.45 + index * step;
              return (
                <div
                  key={index}
                  style={{
                    margin: `${unit * 1.5}px 0`,
                    opacity: interpolate(frame, [from, from + fps * 0.55], [0, 1], CLAMP),
                    transform: `translateX(${interpolate(
                      frame,
                      [from, from + fps * 0.55],
                      [-unit * 1.2, 0],
                      CLAMP,
                    )}px)`,
                  }}
                >
                  <div style={{ ...text, fontSize: unit * 3.2 }}>{item.name}</div>
                  {item.when ? (
                    <div style={{ ...text, color: palette.gold, fontSize: unit * 2,
                      letterSpacing: "0.1em", marginTop: unit * 0.4 }}>
                      {item.when}
                    </div>
                  ) : null}
                  {item.where ? (
                    <div style={{ ...text, fontSize: unit * 1.8, opacity: 0.75,
                      marginTop: unit * 0.3 }}>
                      {item.where}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </AbsoluteFill>
        ) : null}

        <AbsoluteFill
          style={{ ...middle, ...enter(frame, marks.close, fps, unit * 1.2, true) }}
        >
          <div
            style={{
              ...text,
              fontSize: unit * 2.6,
              lineHeight: 1.5,
              letterSpacing: "0.04em",
              // An operator writing "With love," on one line and the family
              // names on the next means two lines, and gets two lines.
              whiteSpace: "pre-line",
            }}
          >
            {closing}
          </div>
        </AbsoluteFill>

        <Corners
          unit={unit}
          colour={palette.gold}
          frame={frame}
          mark={marks.close}
          fps={fps}
        />
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


def invitation_props(
    *,
    config: InvitationConfig,
    photos: list[str],
    palette: dict[str, str],
) -> dict[str, Any]:
    """What the composition is handed: the card's words, colours and pictures.

    The text goes across as props, never as markup. It reaches here from a
    Telegram message, sometimes by way of a model, and React renders a prop as
    a text node whatever it contains, so there is nothing to escape and nothing
    to smuggle in.
    """
    return {
        "photos": photos,
        "palette": palette,
        # The font stack travels with the props rather than living in the
        # component, so the reason for it stays next to the palette it is
        # chosen with. See SERIF above for why the Noto families are in it.
        "font": SERIF,
        "symbol": config.header_symbol,
        "inviteLine": config.invite_line,
        "bride": config.bride,
        "groom": config.groom,
        "joiner": config.joiner,
        "dateLine": config.date_line,
        "timeLine": config.time_line,
        "venue": config.venue,
        "functions": [item.model_dump() for item in config.functions],
        "closing": config.closing,
    }


def merge_details(config: InvitationConfig, raw: str) -> InvitationConfig:
    """Fold an agent's JSON into the card, field by field, ignoring nonsense.

    Validated rather than trusted, like the montage's plan. The text this came
    from was typed by a person into Telegram and passed through a model, so a
    field that is not the right shape is dropped and the value already on the
    node stands. A malformed answer costs an ordinary invitation, never a
    failed run — an operator waiting on a video should not be told about JSON.
    """
    try:
        given = json.loads(raw)
    except ValueError:
        return config
    if not isinstance(given, dict):
        return config

    update: dict[str, Any] = {}
    for field in (
        "invite_line",
        "bride",
        "groom",
        "joiner",
        "date_line",
        "time_line",
        "venue",
        "closing",
        "header_symbol",
    ):
        value = given.get(field)
        if isinstance(value, str) and value.strip():
            update[field] = value.strip()[:200]

    if isinstance(given.get("palette"), str) and given["palette"] in PALETTES:
        update["palette"] = given["palette"]
    if isinstance(given.get("seconds"), (int, float)):
        update["seconds"] = max(MIN_SECONDS, min(MAX_SECONDS, float(given["seconds"])))

    functions: list[Function] = []
    for item in given.get("functions") or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        try:
            functions.append(
                Function(
                    name=str(item["name"])[:40],
                    when=str(item.get("when") or "")[:60],
                    where=str(item.get("where") or "")[:80],
                )
            )
        except ValueError:
            continue
    if functions:
        update["functions"] = functions[:6]

    try:
        return config.model_copy(update=update)
    except ValueError:
        # A field that survived the checks above and still will not validate.
        return config
