"""Wedding invitation films.

What a studio is actually asked for. Not a montage of an event that happened —
a film sent to guests before it, carrying the same information a printed card
carries: whose wedding, when, where, which function is on which day, and who is
inviting you. It is read on a phone, forwarded on WhatsApp, and watched with
the sound off, which decides almost every choice below.

Three of those choices are worth stating.

**Everything is drawn, not photographed.** The ornament is inline SVG on a
timeline, so there is no asset server, no font that might not load, and no
image that renders at a different size on a different machine. A composition
that looks right here looks identical in the worker.

**The type is the design.** An invitation is mostly words, held still long
enough to be read: names large, everything else quiet around them. The motion
exists to bring a line in and let it settle, never to be noticed.

**Watched with the sound off.** Every fact is on screen as text. Music, when
there is any, is decoration rather than delivery.
"""

from __future__ import annotations

import html as html_escape
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

MIN_SECONDS, MAX_SECONDS = 8.0, 60.0

#: An invitation stack is set in a serif. EB Garamond is installed in the
#: worker image; the Noto families carry Tamil and Devanagari so a name in
#: either does not render as boxes. Order matters: the first family that has a
#: glyph wins, per character.
SERIF = '"EB Garamond", "Noto Serif", "Noto Serif Devanagari", "Noto Serif Tamil", Georgia, serif'

PALETTES: dict[str, dict[str, str]] = {
    # The traditional South Indian wedding card: deep maroon, gold, ivory.
    "maroon_gold": {"bg": "#3b0d17", "deep": "#2a0910", "ink": "#fbf1e3", "gold": "#e0b768"},
    # Ivory with gold, for a daytime or Christian ceremony.
    "ivory_gold": {"bg": "#f7f1e6", "deep": "#efe6d6", "ink": "#2e2013", "gold": "#b08game"},
    "emerald_gold": {"bg": "#0f2a22", "deep": "#0a1e18", "ink": "#f3f7f2", "gold": "#d8bf7a"},
    "blush_rose": {"bg": "#2a1119", "deep": "#1e0c12", "ink": "#fdf0f2", "gold": "#e5a3ac"},
    "royal_blue": {"bg": "#101a3a", "deep": "#0a1029", "ink": "#f2f4ff", "gold": "#d9c07c"},
}
# One palette had a typo in its gold; kept honest rather than shipped broken.
PALETTES["ivory_gold"]["gold"] = "#b0873a"

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
        from basivo_orch.flows.nodes.video import VideoRenderConfig, _render

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
        composition = build_invitation(
            config=config,
            photos=list(assets),
            width=width,
            height=height,
            palette=PALETTES[config.palette],
            music=bool(config.music_artifact_id.strip()),
        )

        if music_id := str(render_value(config.music_artifact_id, template)).strip():
            if ctx.load_artifact and (music := await ctx.load_artifact(music_id)):
                assets["music.mp3"] = music

        await ctx.progress(f"Rendering a {config.seconds:g}s invitation")

        data, logs = await _render(
            composition,
            variables={},
            config=VideoRenderConfig(
                template="custom",
                html=composition,
                fps=config.fps,
                quality=config.quality,
                filename="invitation",
            ),
            assets=assets,
        )
        saved = await ctx.save_artifact(
            data, filename="invitation.mp4", content_type="video/mp4", node_id=ctx.node_id
        )
        await ctx.step("invitation.rendered", {**saved, "log_tail": logs[-400:]})
        return NodeResult(
            output={**saved, "seconds": config.seconds, "width": width, "height": height}
        )


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------


def _mandala(size: int, colour: str) -> str:
    """The ring behind the opening line.

    Drawn as arcs and petals rather than shipped as an image: it scales to any
    frame, weighs nothing, and can be animated stroke by stroke — the ring
    drawing itself is what gives an invitation its first two seconds.
    """
    petals = "".join(
        f'<path d="M50 14 C 58 26, 58 34, 50 44 C 42 34, 42 26, 50 14 Z" '
        f'transform="rotate({angle} 50 50)" fill="none" stroke="{colour}" '
        f'stroke-width="1.2" opacity="0.9"/>'
        for angle in range(0, 360, 30)
    )
    return f"""<svg class="mandala" width="{size}" height="{size}" viewBox="0 0 100 100">
      <circle cx="50" cy="50" r="46" fill="none" stroke="{colour}" stroke-width="1.1"
              stroke-dasharray="1.8 2.6" opacity="0.85"/>
      <circle cx="50" cy="50" r="39" fill="none" stroke="{colour}" stroke-width="1.6"/>
      <circle cx="50" cy="50" r="30" fill="none" stroke="{colour}" stroke-width="0.9"
              opacity="0.75"/>
      {petals}
    </svg>"""


def _divider(colour: str) -> str:
    """A rule with a diamond in the middle. The full stop of a wedding card."""
    return (
        f'<svg class="divider" viewBox="0 0 200 12" preserveAspectRatio="xMidYMid meet">'
        f'<line x1="10" y1="6" x2="86" y2="6" stroke="{colour}" stroke-width="0.8"/>'
        f'<path d="M100 1 L106 6 L100 11 L94 6 Z" fill="{colour}"/>'
        f'<line x1="114" y1="6" x2="190" y2="6" stroke="{colour}" stroke-width="0.8"/>'
        f"</svg>"
    )


def _corners(colour: str) -> str:
    """Floral corners, which is what makes a plain frame read as a card."""
    one = (
        f'<svg class="corner" viewBox="0 0 100 100"><path d="M4 40 C 4 16, 16 4, 40 4" '
        f'fill="none" stroke="{colour}" stroke-width="1.4"/>'
        f'<path d="M4 62 C 4 26, 26 4, 62 4" fill="none" stroke="{colour}" '
        f'stroke-width="0.7" opacity="0.65"/>'
        f'<circle cx="12" cy="12" r="2.4" fill="{colour}"/></svg>'
    )
    return "".join(f'<div class="c c{index}">{one}</div>' for index in range(4))


def build_invitation(
    *,
    config: InvitationConfig,
    photos: list[str],
    width: int,
    height: int,
    palette: dict[str, str],
    music: bool,
) -> str:
    """The whole film, as one HTML file.

    Timing is proportional rather than fixed, so a 12 second cut and a 30
    second cut are the same film at different paces instead of the same scenes
    with a longer gap at the end. The proportions come from what a reader needs:
    a name can be taken in at a glance, a schedule of five functions cannot.
    """
    total = float(config.seconds)

    def escape(value: str) -> str:
        """Escape first, then honour line breaks.

        An operator writing "With love," on one line and the family names on
        the next means two lines. They must not be able to mean anything else:
        this text reaches the composition from a Telegram message, sometimes by
        way of a model, and both are places markup could be smuggled in from.
        """
        return html_escape.escape(value).replace("\n", "<br>")

    # Scene boundaries as fractions of the whole.
    has_functions = bool(config.functions)
    weights = {
        "open": 0.18,
        "names": 0.30,
        "when": 0.20,
        "functions": 0.22 if has_functions else 0.0,
        "close": 0.12 if has_functions else 0.32,
    }
    marks: dict[str, tuple[float, float]] = {}
    cursor = 0.0
    for scene, weight in weights.items():
        length = round(total * weight, 3)
        marks[scene] = (round(cursor, 3), length)
        cursor += length

    gold, ink, bg, deep = palette["gold"], palette["ink"], palette["bg"], palette["deep"]
    unit = height / 100  # everything scales with the frame

    # --- layers -----------------------------------------------------------
    photo_class = "with-photo" if photos else ""
    photo_layers = "".join(
        f'<div class="photo" id="ph{index}"><img src="{name}" alt=""></div>'
        for index, name in enumerate(photos)
    )

    names_block = (
        f'<div class="names" id="names">'
        f'<div class="n">{escape(config.bride)}</div>'
        f'<div class="j">{escape(config.joiner)}</div>'
        f'<div class="n">{escape(config.groom)}</div>'
        f"</div>"
    )

    functions_rows = "".join(
        f'<div class="fn" id="fn{index}">'
        f'<div class="fname">{escape(item.name)}</div>'
        f'<div class="fwhen">{escape(item.when)}</div>'
        + (f'<div class="fwhere">{escape(item.where)}</div>' if item.where else "")
        + "</div>"
        for index, item in enumerate(config.functions)
    )

    functions_block = (
        '<div class="center"><div id="functions">'
        '<div id="fnhead">Celebrations</div>'
        f"{functions_rows}</div></div>"
        if has_functions
        else ""
    )

    # --- timeline ----------------------------------------------------------
    lines: list[str] = []

    def at(scene: str) -> tuple[float, float]:
        return marks[scene]

    start, length = at("open")
    lines += [
        f"tl.fromTo('#mandala', {{opacity:0, scale:0.86, rotate:-8}}, "
        f"{{opacity:1, scale:1, rotate:0, duration:{length * 0.55:.3f}, "
        f"ease:'power2.out'}}, {start:.3f});",
        f"tl.fromTo('#invite', {{opacity:0, y:{unit * 1.4:.1f}}}, "
        f"{{opacity:1, y:0, duration:{length * 0.4:.3f}, ease:'power2.out'}}, "
        f"{start + length * 0.35:.3f});",
        f"tl.to(['#mandala', '#invite'], {{opacity:0, duration:0.55, ease:'power1.in'}}, "
        f"{start + length - 0.45:.3f});",
    ]
    if config.header_symbol:
        lines.append(
            f"tl.fromTo('#symbol', {{opacity:0}}, {{opacity:1, duration:0.8}}, {start:.3f});"
        )
        lines.append(f"tl.to('#symbol', {{opacity:0, duration:0.5}}, {start + length - 0.45:.3f});")

    start, length = at("names")
    if photos:
        # The photograph carries this scene, so it drifts for its whole length
        # rather than settling — stillness here reads as a frozen video.
        lines += [
            f"tl.fromTo('#ph0', {{opacity:0}}, {{opacity:1, duration:0.9, ease:'power1.out'}}, "
            f"{start - 0.5:.3f});",
            f"tl.fromTo('#ph0 img', {{transform:'scale(1.04) translateY(1%)'}}, "
            f"{{transform:'scale(1.14) translateY(-1%)', duration:{length + 1.2:.3f}, "
            f"ease:'none'}}, {start - 0.5:.3f});",
        ]
    if photos:
        lines.append(
            f"tl.to('#ph0', {{opacity:0, duration:0.7, ease:'power1.in'}}, "
            f"{start + length - 0.55:.3f});"
        )
    lines += [
        f"tl.fromTo('#names', {{opacity:0, y:{unit * 2.2:.1f}}}, "
        f"{{opacity:1, y:0, duration:1.1, ease:'power3.out'}}, {start + 0.25:.3f});",
        f"tl.fromTo('#namesrule', {{scaleX:0}}, {{scaleX:1, duration:0.9, "
        f"ease:'power2.out'}}, {start + 0.75:.3f});",
        f"tl.to(['#names', '#namesrule'], {{opacity:0, duration:0.6, ease:'power1.in'}}, "
        f"{start + length - 0.5:.3f});",
    ]

    start, length = at("when")
    if len(photos) > 1:
        lines += [
            f"tl.fromTo('#ph1', {{opacity:0}}, {{opacity:1, duration:0.9}}, {start - 0.4:.3f});",
            f"tl.fromTo('#ph1 img', {{transform:'scale(1.12) translateX(1%)'}}, "
            f"{{transform:'scale(1.03) translateX(-1%)', duration:{length + 1.0:.3f}, "
            f"ease:'none'}}, {start - 0.4:.3f});",
        ]
    if len(photos) > 1:
        lines.append(
            f"tl.to('#ph1', {{opacity:0, duration:0.7, ease:'power1.in'}}, "
            f"{start + length - 0.55:.3f});"
        )
    lines += [
        f"tl.fromTo('#when', {{opacity:0, y:{unit * 1.6:.1f}}}, {{opacity:1, y:0, "
        f"duration:0.9, ease:'power2.out'}}, {start:.3f});",
        f"tl.to('#when', {{opacity:0, duration:0.55, ease:'power1.in'}}, "
        f"{start + length - 0.45:.3f});",
    ]

    if has_functions:
        start, length = at("functions")
        lines.append(
            f"tl.fromTo('#fnhead', {{opacity:0}}, {{opacity:1, duration:0.6}}, {start:.3f});"
        )
        # Staggered, because a schedule that appears all at once is a wall.
        per_row = min(0.5, (length - 1.4) / max(1, len(config.functions)))
        for index in range(len(config.functions)):
            lines.append(
                f"tl.fromTo('#fn{index}', {{opacity:0, x:{-unit * 1.2:.1f}}}, "
                f"{{opacity:1, x:0, duration:0.55, ease:'power2.out'}}, "
                f"{start + 0.45 + index * per_row:.3f});"
            )
        lines.append(
            f"tl.to('#functions', {{opacity:0, duration:0.55, ease:'power1.in'}}, "
            f"{start + length - 0.45:.3f});"
        )

    start, length = at("close")
    lines += [
        f"tl.fromTo('#close', {{opacity:0, y:{unit * 1.2:.1f}}}, {{opacity:1, y:0, "
        f"duration:0.9, ease:'power2.out'}}, {start:.3f});",
        # The frame draws itself in on the closing card, which is the flourish
        # people remember and the reason the corners exist.
        f"tl.fromTo('.c', {{opacity:0, scale:0.8}}, {{opacity:1, scale:1, "
        f"duration:0.8, stagger:0.12, ease:'power2.out'}}, {start + 0.2:.3f});",
    ]

    audio = (
        '<audio data-audio-src="music.mp3" data-audio-volume="0.75" '
        f'data-audio-fade-out="2" data-audio-duration="{total:.3f}"></audio>'
        if music
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin:0; background:{bg}; }}
  #stage {{ position:relative; width:{width}px; height:{height}px; overflow:hidden;
    background:radial-gradient(120% 90% at 50% 20%, {bg} 0%, {deep} 100%);
    color:{ink}; font-family:{SERIF}; }}
  /* The photograph takes the upper two thirds and the words sit below it.
     The first version washed the picture out under full-frame type, which is
     the one thing a photography studio will not accept: they are selling the
     photograph, and an invitation that hides the couple behind a gradient is
     worse than one with no picture at all. */
  .photo {{ position:absolute; left:0; right:0; top:0; height:64%; opacity:0; }}
  .photo img {{ width:100%; height:100%; object-fit:cover; object-position:center 32%;
    will-change:transform; }}
  /* Only the edges are darkened: enough to seat the picture in the page and
     carry it into the background, never across the faces. */
  .photo::after {{ content:''; position:absolute; inset:0;
    background:linear-gradient(180deg, {deep}88 0%, transparent 20%,
      transparent 46%, {deep}55 68%, {deep}cc 86%, {bg} 100%); }}
  .center {{ position:absolute; inset:0; display:flex; flex-direction:column;
    align-items:center; justify-content:center; text-align:center;
    padding:0 {unit * 9:.0f}px; }}
  /* When a photograph is showing, the type moves to the lower third rather
     than sitting on top of it. */
  .with-photo {{ justify-content:flex-end; padding-bottom:{unit * 11:.0f}px; }}
  #mandala {{ opacity:0; }}
  #invite {{ opacity:0; margin-top:{unit * 2.4:.0f}px; font-size:{unit * 2.5:.0f}px;
    letter-spacing:.20em; text-transform:uppercase; color:{gold}; }}
  #symbol {{ opacity:0; font-size:{unit * 3.4:.0f}px; color:{gold};
    margin-bottom:{unit * 1.6:.0f}px; }}
  .names {{ opacity:0; }}
  .names .n {{ font-size:{unit * 7.2:.0f}px; line-height:1.06; }}
  .names .j {{ font-size:{unit * 3.6:.0f}px; color:{gold}; margin:{unit * 0.8:.0f}px 0; }}
  #namesrule {{ width:{unit * 22:.0f}px; height:2px; background:{gold};
    margin-top:{unit * 2.2:.0f}px; transform:scaleX(0); }}
  #when {{ opacity:0; }}
  #when .d {{ font-size:{unit * 4.0:.0f}px; letter-spacing:.03em; }}
  #when .t {{ font-size:{unit * 2.4:.0f}px; color:{gold}; margin-top:{unit * 1.0:.0f}px;
    letter-spacing:.18em; text-transform:uppercase; }}
  #when .v {{ font-size:{unit * 2.3:.0f}px; margin-top:{unit * 2.0:.0f}px; opacity:.92;
    line-height:1.45; }}
  #functions {{ width:100%; }}
  #fnhead {{ opacity:0; font-size:{unit * 2.2:.0f}px; letter-spacing:.22em;
    text-transform:uppercase; color:{gold}; margin-bottom:{unit * 2.4:.0f}px; }}
  .fn {{ opacity:0; margin:{unit * 1.5:.0f}px 0; }}
  .fname {{ font-size:{unit * 3.2:.0f}px; }}
  .fwhen {{ font-size:{unit * 2.0:.0f}px; color:{gold}; letter-spacing:.1em;
    margin-top:{unit * 0.4:.0f}px; }}
  .fwhere {{ font-size:{unit * 1.8:.0f}px; opacity:.75; margin-top:{unit * 0.3:.0f}px; }}
  #close {{ opacity:0; font-size:{unit * 2.6:.0f}px; line-height:1.5; letter-spacing:.04em; }}
  .divider {{ width:{unit * 24:.0f}px; margin:{unit * 1.8:.0f}px auto; display:block; }}
  .c {{ position:absolute; width:{unit * 11:.0f}px; height:{unit * 11:.0f}px; opacity:0; }}
  .c svg {{ width:100%; height:100%; }}
  .c0 {{ top:{unit * 4:.0f}px; left:{unit * 4:.0f}px; }}
  .c1 {{ top:{unit * 4:.0f}px; right:{unit * 4:.0f}px; transform:scaleX(-1); }}
  .c2 {{ bottom:{unit * 4:.0f}px; left:{unit * 4:.0f}px; transform:scaleY(-1); }}
  .c3 {{ bottom:{unit * 4:.0f}px; right:{unit * 4:.0f}px; transform:scale(-1,-1); }}
</style></head>
<body>
<div id="stage" data-composition-id="invitation" data-start="0" data-duration="{total:.3f}"
     data-width="{width}" data-height="{height}" data-fps="{config.fps}"
     data-composition-variables='{{}}'>
  <div class="clip layer" data-start="0" data-duration="{total:.3f}" data-track-index="0">
    {photo_layers}
    <div class="center">
      {f'<div id="symbol">{escape(config.header_symbol)}</div>' if config.header_symbol else ""}
      <div id="mandala">{_mandala(int(unit * 34), gold)}</div>
      <div id="invite">{escape(config.invite_line)}</div>
    </div>
    <div class="center {photo_class}">
      {names_block}
      <div id="namesrule"></div>
    </div>
    <div class="center {photo_class}">
      <div id="when">
        <div class="d">{escape(config.date_line)}</div>
        {f'<div class="t">{escape(config.time_line)}</div>' if config.time_line else ""}
        {_divider(gold)}
        {f'<div class="v">{escape(config.venue)}</div>' if config.venue else ""}
      </div>
    </div>
    {functions_block}
    <div class="center">
      <div id="close">{escape(config.closing)}</div>
    </div>
    {_corners(gold)}
    {audio}
  </div>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    {" ".join(lines)}
    window.__timelines = window.__timelines || {{}};
    window.__timelines['invitation'] = tl;
  </script>
</div>
</body></html>"""


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
