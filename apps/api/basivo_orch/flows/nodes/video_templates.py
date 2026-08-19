"""Starter compositions, so nobody meets this node with an empty HTML box.

Each one is a complete HyperFrames composition whose text, colours and timing
come from `data-composition-variables`. A user picks a template and fills the
values; an agent upstream can fill them instead, which is the whole point —
writing a paragraph of copy is a thing a model does well, and authoring a
seekable GSAP timeline from scratch is not.

Everything is inline: no build step, no asset server, no font that might not
load. A composition that renders on a laptop renders identically in a worker
container, which is the property the whole feature rests on.
"""

from __future__ import annotations

import json

_SHELL = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; background: #000; }}
  #stage {{
    position: relative; width: {width}px; height: {height}px; overflow: hidden;
    font-family: Inter, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: var(--ink, #fff); background: var(--bg, #0b0b12);
  }}
  .layer {{ position: absolute; inset: 0; }}
  {css}
</style></head>
<body>
<div id="stage" data-composition-id="{id}" data-start="0" data-duration="{duration}"
     data-width="{width}" data-height="{height}" data-fps="30"
     data-composition-variables='{variables}'>
  <div class="clip layer" data-start="0" data-duration="{duration}" data-track-index="0">
{body}
  </div>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
  <script>
    const stage = document.getElementById('stage');
    const v = (window.__hyperframes && window.__hyperframes.getVariables)
      ? window.__hyperframes.getVariables() : {{}};
    for (const [key, value] of Object.entries(v)) {{
      const node = document.querySelector('[data-var="' + key + '"]');
      if (node) node.textContent = value;
      // Set on the stage as well as the root: the gradients live on #stage,
      // and a value that only reaches :root leaves them resolving to nothing.
      document.documentElement.style.setProperty('--' + key, value);
      stage.style.setProperty('--' + key, value);
    }}
    const tl = gsap.timeline({{ paused: true }});
{timeline}
    window.__timelines = window.__timelines || {{}};
    window.__timelines['{id}'] = tl;
  </script>
</div>
</body></html>
"""


def _compose(
    *,
    id: str,
    duration: float,
    width: int,
    height: int,
    css: str,
    body: str,
    timeline: str,
    variables: dict[str, str],
) -> str:
    # json.dumps rather than a hand-written literal: the defaults live in the
    # attribute as JSON, and one missing escape there breaks the whole
    # composition at render time with a parse error nobody can place.
    return _SHELL.format(
        id=id,
        duration=duration,
        width=width,
        height=height,
        css=css,
        body=body,
        timeline=timeline,
        variables=json.dumps(variables),
    )


PRODUCT_PROMO = _compose(
    id="product_promo",
    duration=6,
    width=1920,
    height=1080,
    variables={
        "headline": "Ship it before standup",
        "subline": "Automations that finish the job",
        "brand": "Basivo",
        "cta": "basivo.in",
        "bg": "#0f172a",
        "accent": "#7857ff",
    },
    css="""
    #stage { background: linear-gradient(140deg,
             var(--bg, #0f172a) 0%, #2e1065 55%, var(--accent, #7857ff) 100%); }
    .brand { position: absolute; top: 84px; left: 110px; letter-spacing: 8px;
             text-transform: uppercase; font-size: 26px; opacity: .8; }
    .mid { position: absolute; left: 110px; right: 110px; top: 50%; transform: translateY(-50%); }
    h1 { font-size: 128px; line-height: 1.02; margin: 0; font-weight: 800; letter-spacing: -4px; }
    p  { font-size: 44px; margin: 32px 0 0; opacity: .85; }
    .cta { position: absolute; bottom: 84px; left: 110px; font-size: 32px; opacity: .8; }
    .rule { position: absolute; bottom: 150px; left: 110px; height: 5px; width: 320px;
            background: #fff; opacity: .9; transform-origin: left center; }
    """,
    body="""    <div class="brand" data-var="brand">Basivo</div>
    <div class="mid">
      <h1 data-var="headline">Ship it before standup</h1>
      <p data-var="subline">Automations that finish the job</p>
    </div>
    <div class="rule"></div>
    <div class="cta" data-var="cta">basivo.in</div>""",
    timeline="""    tl.from('.brand', { opacity: 0, y: -24, duration: .7 }, 0.15)
      .from('h1',      { opacity: 0, y: 60, duration: 1.0 }, 0.35)
      .from('p',       { opacity: 0, y: 30, duration: .9 }, 0.75)
      .from('.rule',   { scaleX: 0, duration: .9 }, 1.1)
      .from('.cta',    { opacity: 0, duration: .8 }, 1.4)
      .to('.mid',      { y: '-=26', duration: 3.2 }, 1.6);""",
)


ANNOUNCEMENT = _compose(
    id="announcement",
    duration=5,
    width=1080,
    height=1080,
    variables={
        "kicker": "New",
        "headline": "Auto-fix, now with screenshots",
        "detail": "Open an issue with a picture. Get a pull request.",
        "bg": "#0b0b12",
        "accent": "#22c55e",
    },
    css="""
    #stage { background: radial-gradient(circle at 30% 20%, #1e293b 0%, var(--bg, #0b0b12) 60%); }
    .pill { position: absolute; top: 90px; left: 90px; padding: 12px 26px; border-radius: 999px;
            background: var(--accent, #22c55e); color: #05130a; font-weight: 700; font-size: 26px;
            letter-spacing: 2px; text-transform: uppercase; }
    .body { position: absolute; left: 90px; right: 90px; top: 44%; transform: translateY(-40%); }
    h1 { font-size: 96px; line-height: 1.04; margin: 0; font-weight: 800; letter-spacing: -3px; }
    p  { font-size: 38px; margin: 28px 0 0; opacity: .8; }
    .glow { position: absolute; width: 620px; height: 620px; right: -160px; bottom: -200px;
            border-radius: 50%; background: var(--accent, #22c55e);
            filter: blur(120px); opacity: .35; }
    """,
    body="""    <div class="glow"></div>
    <div class="pill" data-var="kicker">New</div>
    <div class="body">
      <h1 data-var="headline">Auto-fix, now with screenshots</h1>
      <p data-var="detail">Open an issue with a picture. Get a pull request.</p>
    </div>""",
    timeline="""    tl.from('.pill', { opacity: 0, scale: .8, duration: .6 }, 0.1)
      .from('h1',    { opacity: 0, y: 44, duration: .9 }, 0.3)
      .from('p',     { opacity: 0, y: 24, duration: .8 }, 0.7)
      .fromTo('.glow',
              { scale: .85, opacity: .2 },
              { scale: 1.08, opacity: .4, duration: 4 }, 0);""",
)


STAT_REVEAL = _compose(
    id="stat_reveal",
    duration=5,
    width=1920,
    height=1080,
    variables={
        "stat": "666s",
        "label": "Issue to reviewed pull request",
        "note": "Fully automated, human merges",
        "bg": "#050509",
        "accent": "#38bdf8",
    },
    css="""
    #stage { background: var(--bg, #050509); display: grid; place-items: center; }
    .grid { position: absolute; inset: 0; opacity: .18;
            background-image: linear-gradient(var(--accent, #38bdf8) 1px, transparent 1px),
                              linear-gradient(90deg, var(--accent, #38bdf8) 1px, transparent 1px);
            background-size: 90px 90px; }
    .center { position: relative; text-align: center; }
    .stat { font-size: 260px; font-weight: 800; letter-spacing: -10px; margin: 0;
            color: var(--accent, #38bdf8); }
    .label { font-size: 46px; margin: 10px 0 0; opacity: .9; }
    .note { font-size: 28px; margin: 26px 0 0; opacity: .55; }
    """,
    body="""    <div class="grid"></div>
    <div class="center">
      <p class="stat" data-var="stat">666s</p>
      <p class="label" data-var="label">Issue to reviewed pull request</p>
      <p class="note" data-var="note">Fully automated, human merges</p>
    </div>""",
    timeline="""    tl.from('.stat',
              { opacity: 0, scale: .78, duration: 1.0, ease: 'back.out(1.6)' }, 0.2)
      .from('.label', { opacity: 0, y: 26, duration: .8 }, 0.8)
      .from('.note',  { opacity: 0, duration: .8 }, 1.1)
      .to('.grid',    { backgroundPositionX: '90px', duration: 4.6, ease: 'none' }, 0);""",
)


ANIME_TITLE = _compose(
    id="anime_title",
    duration=5,
    width=1920,
    height=1080,
    variables={
        "title": "BASIVO",
        "subtitle": "Episode 01 — The Night Shift",
        "bg": "#120018",
        "accent": "#ff2d95",
    },
    css="""
    #stage { background: linear-gradient(180deg, var(--bg, #120018) 0%, #2b0033 60%, #46003f 100%);
             display: grid; place-items: center; }
    .rays { position: absolute; inset: -30%; opacity: .35;
            background: repeating-conic-gradient(from 0deg,
              var(--accent, #ff2d95) 0deg 4deg, transparent 4deg 12deg); }
    .vignette { position: absolute; inset: 0;
            background: radial-gradient(circle at 50% 45%, transparent 35%, rgba(0,0,0,.85) 100%); }
    .center { position: relative; text-align: center; }
    .title { font-size: 230px; font-weight: 800; margin: 0; letter-spacing: 22px;
             text-shadow: 0 0 40px var(--accent, #ff2d95), 0 8px 0 rgba(0,0,0,.5);
             -webkit-text-stroke: 3px rgba(255,255,255,.85); color: transparent; }
    .subtitle { font-size: 40px; margin: 22px 0 0; letter-spacing: 10px;
                text-transform: uppercase; opacity: .9; }
    .slash { position: absolute; top: 50%; left: -20%; width: 140%; height: 6px;
             background: #fff; opacity: .9; transform: rotate(-8deg); }
    """,
    body="""    <div class="rays"></div>
    <div class="vignette"></div>
    <div class="center">
      <p class="title" data-var="title">BASIVO</p>
      <p class="subtitle" data-var="subtitle">Episode 01 — The Night Shift</p>
    </div>
    <div class="slash"></div>""",
    timeline="""    tl.fromTo('.rays', { rotate: 0 }, { rotate: 26, duration: 5, ease: 'none' }, 0)
      .from('.slash', { scaleX: 0, transformOrigin: 'left center', duration: .35 }, 0.25)
      .to('.slash',   { opacity: 0, duration: .3 }, 0.7)
      .from('.title', { opacity: 0, scale: 1.35, duration: .5, ease: 'power4.out' }, 0.6)
      .from('.subtitle', { opacity: 0, y: 22, duration: .7 }, 1.0)
      .to('.title',   { letterSpacing: '30px', duration: 4, ease: 'none' }, 1.0);""",
)


TEMPLATES: dict[str, str] = {
    "product_promo": PRODUCT_PROMO,
    "announcement": ANNOUNCEMENT,
    "stat_reveal": STAT_REVEAL,
    "anime_title": ANIME_TITLE,
}
