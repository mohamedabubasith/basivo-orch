"""Rendering video through HyperFrames.

Most of these are fast: they check the contract around the renderer — which
command is invoked, what the templates promise, what is refused before several
minutes of work begins. One actually renders an MP4 and is marked `slow`,
because the only real proof that a composition works is a file with frames in
it.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.video import (
    MAX_DURATION_SECONDS,
    VideoRenderConfig,
    VideoRenderNode,
    hyperframes_command,
)
from basivo_orch.flows.nodes.video_templates import TEMPLATES


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.saved: list[bytes] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(recorder: _Recorder, **overrides) -> NodeContext:
    async def save_artifact(data: bytes, *, filename: str, content_type: str, node_id=None):
        recorder.saved.append(data)
        return {
            "artifact_id": "vid-1",
            "url": "/api/v1/orgs/o/artifacts/vid-1",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        }

    fields = dict(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="video",
        node_name="Video",
        attempt=1,
        input={"headline": "Written by an agent"},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=None,
        http=None,
        save_artifact=save_artifact,
    )
    fields.update(overrides)
    return NodeContext(**fields)


# ---------------------------------------------------------------------------
# The templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_is_a_valid_seekable_composition(name: str):
    """The four things HyperFrames needs, present in every starter.

    A composition missing its timeline renders as a still image and nobody can
    tell why; missing dimensions renders at the wrong size. Cheaper to assert
    here than to discover after a four-minute render.
    """
    html = TEMPLATES[name]
    assert f'data-composition-id="{name}"' in html
    assert "data-duration=" in html and "data-width=" in html and "data-height=" in html
    assert "window.__timelines" in html, "no seekable timeline — this would render as a still"
    assert "gsap" in html

    variables = json.loads(re.search(r"data-composition-variables='([^']+)'", html).group(1))
    assert variables, "a template with no variables cannot be filled in"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_colour_has_a_fallback(name: str):
    """A composition rendered with only *some* variables must still look like
    the design. `var(--bg)` with nothing behind it resolves to nothing, the
    gradient becomes invalid, and the video comes out black — which is exactly
    what happened the first time these were rendered."""
    for reference in re.findall(r"var\(--(\w+)([^)]*)\)", TEMPLATES[name]):
        name_, rest = reference
        assert "," in rest, f"--{name_} has no fallback value"


def test_a_custom_video_needs_its_html():
    with pytest.raises(ValueError, match="composition HTML"):
        VideoRenderConfig(template="custom")


def test_variables_must_be_a_json_object():
    with pytest.raises(ValueError, match="JSON"):
        VideoRenderConfig(template="announcement", variables="not json")
    with pytest.raises(ValueError, match="JSON object"):
        VideoRenderConfig(template="announcement", variables="[1, 2]")
    # A template reference is not JSON yet; it is checked after rendering.
    VideoRenderConfig(template="announcement", variables='{"headline": "{{ input.headline }}"}')


# ---------------------------------------------------------------------------
# Around the renderer
# ---------------------------------------------------------------------------


def test_the_renderer_is_pinned_and_overridable(monkeypatch):
    """A silent upgrade mid-project is a changed video nobody asked for."""
    monkeypatch.delenv("BASIVO_HYPERFRAMES_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert hyperframes_command() == ["npx", "--yes", "hyperframes@0.8.3"]

    monkeypatch.setenv("BASIVO_HYPERFRAMES_BIN", "/opt/hf/bin/hyperframes")
    assert hyperframes_command() == ["/opt/hf/bin/hyperframes"]


async def test_a_composition_longer_than_the_limit_is_refused_before_rendering():
    recorder = _Recorder()
    long_html = TEMPLATES["announcement"].replace(
        'data-duration="5"', f'data-duration="{MAX_DURATION_SECONDS + 30}"'
    )
    with pytest.raises(NodeError, match=f"limit is {MAX_DURATION_SECONDS}s"):
        await VideoRenderNode().run(
            VideoRenderConfig(template="custom", html=long_html), make_context(recorder)
        )
    assert not recorder.steps, "work started before the length was checked"


async def test_variables_that_do_not_survive_templating_say_so():
    recorder = _Recorder()
    with pytest.raises(NodeError, match="did not come out as JSON"):
        await VideoRenderNode().run(
            VideoRenderConfig(
                template="announcement",
                # An unquoted reference produces invalid JSON once filled in.
                variables='{"headline": {{ input.headline }}}',
            ),
            make_context(recorder),
        )


async def test_a_missing_renderer_explains_how_to_install_it(monkeypatch):
    monkeypatch.setenv("BASIVO_HYPERFRAMES_BIN", "/nonexistent/hyperframes")
    recorder = _Recorder()
    with pytest.raises(NodeError, match="not installed"):
        await VideoRenderNode().run(
            VideoRenderConfig(template="announcement"), make_context(recorder)
        )


# ---------------------------------------------------------------------------
# The real thing
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("BASIVO_HYPERFRAMES_BIN"),
    reason="set BASIVO_HYPERFRAMES_BIN to run a real render",
)
async def test_it_renders_a_real_mp4_from_a_template():
    recorder = _Recorder()
    result = await VideoRenderNode().run(
        VideoRenderConfig(
            template="announcement",
            variables='{"headline": "{{ input.headline }}"}',
            quality="draft",
            fps=12,
        ),
        make_context(recorder),
    )

    video = recorder.saved[0]
    # ftyp box near the start is what makes a file an MP4.
    assert b"ftyp" in video[:64], "that is not an MP4"
    assert result.output["duration_seconds"] == 5.0
    assert result.output["format"] == "mp4"
    assert recorder.data_for("video.started")[0]["variables"] == ["headline"]


# ---------------------------------------------------------------------------
# Compositions an agent wrote
# ---------------------------------------------------------------------------


def test_markdown_fences_are_unwrapped():
    """Models are told not to fence their answer and do it anyway. Rendering
    the fence produces a video of the literal characters ```html."""
    from basivo_orch.flows.nodes.video import strip_code_fences

    assert strip_code_fences("```html\n<div>hi</div>\n```") == "<div>hi</div>"
    assert strip_code_fences("```\n<div>hi</div>\n```") == "<div>hi</div>"
    assert strip_code_fences("<div>hi</div>") == "<div>hi</div>"


def test_a_composition_with_no_timeline_is_caught_before_rendering():
    """The worst failure mode, because it is silent: a composition with no
    exposed timeline renders *successfully* as a motionless video."""
    from basivo_orch.flows.nodes.video import composition_problems

    still = '<div id="stage" data-composition-id="p" data-duration="5"><h1>Hi</h1></div>'
    assert composition_problems(still) == [
        "missing the paused GSAP timeline exposed on window.__timelines"
    ]
    assert composition_problems(TEMPLATES["announcement"]) == []


async def test_an_agent_written_composition_that_is_broken_fails_with_advice():
    recorder = _Recorder()
    with pytest.raises(NodeError, match="will not render as video"):
        await VideoRenderNode().run(
            VideoRenderConfig(template="custom", html="<h1>just a heading</h1>"),
            make_context(recorder),
        )


def test_the_composition_instructions_state_the_rules_that_matter():
    """These ship with the product because every rule is a way a model-written
    composition fails, and users should not have to rediscover them."""
    from basivo_orch.flows.nodes.video import COMPOSITION_INSTRUCTIONS

    for rule in ("window.__timelines", "data-duration", "gsap", "No markdown fences"):
        assert rule in COMPOSITION_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


def _words(*items):
    return [{"word": word, "start": start, "end": end} for word, start, end in items]


def test_caption_lines_break_on_sentences_before_word_count():
    """A caption that reads "...yourself. Connect a" is harder to read than one
    that stops where the speaker stopped."""
    from basivo_orch.flows.nodes.video import caption_lines

    lines = caption_lines(
        _words(
            ("Do", 0.0, 0.2),
            ("it.", 0.3, 0.6),
            ("Then", 0.7, 0.9),
            ("ship", 1.0, 1.2),
            ("it", 1.3, 1.4),
            ("today", 1.5, 1.8),
            ("please", 1.9, 2.1),
            ("now", 2.2, 2.4),
        )
    )
    assert [" ".join(w["word"] for w in line["words"]) for line in lines] == [
        "Do it.",
        "Then ship it today please now",
    ]
    assert lines[0]["start"] == 0.0 and lines[0]["end"] == 0.6


def test_one_line_never_hides_after_the_next_one_appears():
    """The bug this guards was visible in a real render: two caption lines drawn
    on the same frame, their words interleaved into nonsense. Lines linger past
    their last word so they do not blink out on the final syllable — but never
    past the moment the next line starts."""
    from basivo_orch.flows.nodes.video import caption_lines, caption_script

    lines = caption_lines(
        _words(
            ("one", 0.0, 0.5),
            ("two", 0.6, 1.0),
            ("three", 1.1, 1.5),
            ("four", 1.6, 2.0),
            ("five", 2.1, 2.4),
            ("six", 2.5, 2.52),
            ("seven", 2.55, 3.0),
        ),
    )
    assert len(lines) == 2, "six words per line, so seven words is two lines"
    script = caption_script("promo", lines)

    # Line 0's last word ends at 2.52 and line 1 starts at 2.55, so the usual
    # 0.08s linger is clamped rather than overlapping.
    assert 'tl.set("#hf-line-0",{opacity:0},2.549)' in script
    assert 'tl.set("#hf-line-1",{opacity:1},2.55)' in script


def test_the_caption_layer_carries_its_own_scrim():
    """Not decoration. An agent told not to write its own subtitles wrote them
    anyway, in the same band, and the frame showed both sets of words on top of
    each other. The scrim means the worst case is a hidden line, not an
    unreadable one — and it is what makes white text legible over a light
    composition at all."""
    from basivo_orch.flows.nodes.video import narration_markup

    markup = narration_markup(
        audio_name="n.wav",
        audio_seconds=2.0,
        lines=[{"start": 0.1, "end": 1.0, "words": _words(("hi", 0.1, 0.5))}],
        width=1920,
        height=1080,
    )
    assert "linear-gradient" in markup and "rgba(0,0,0,0.86)" in markup
    assert markup.count("<div") == markup.count("</div>"), "unbalanced markup breaks the layout"
    # Above everything the composition can produce.
    assert "z-index:2147483000" in markup


def test_captions_are_driven_by_the_timeline_not_by_css():
    """The renderer produces frames by SEEKING the composition's timeline, so a
    CSS animation would render as one frozen frame for the whole video."""
    from basivo_orch.flows.nodes.video import caption_script

    script = caption_script(
        "promo", [{"start": 0.2, "end": 1.0, "words": _words(("go", 0.2, 0.9))}]
    )
    assert "window.__timelines" in script
    assert "animation" not in script and "@keyframes" not in script


def test_narration_survives_a_composition_with_no_recognisable_stage():
    """A caption layer with nothing to attach to must not cost the voice."""
    from basivo_orch.flows.nodes.video import inject_narration

    html, captioned, _ = inject_narration(
        "<html><body><p>no stage here</p></body></html>",
        audio_name="n.wav",
        audio_seconds=2.0,
        lines=[{"start": 0.1, "end": 1.0, "words": _words(("hi", 0.1, 0.5))}],
        width=640,
        height=360,
    )
    assert captioned is False
    assert '<audio src="n.wav"' in html, "the voice does not depend on the stage"
    assert "hf-captions" not in html


def test_the_declared_duration_is_widened_to_fit_the_voice():
    """Otherwise the render cuts the narration off mid-word."""
    from basivo_orch.flows.nodes.video import ensure_duration

    short = '<div id="stage" data-composition-id="p" data-duration="4" data-fps="30">'
    widened, changed = ensure_duration(short, 6.3)
    assert changed and 'data-duration="6.3"' in widened

    # Already long enough: left exactly as authored.
    same, changed = ensure_duration(
        '<div id="stage" data-composition-id="p" data-duration="30" data-fps="30">', 20.5
    )
    assert changed is False and 'data-duration="30"' in same


def test_the_probe_looks_often_enough_to_notice_a_gap():
    """Three sample points missed a composition that was blank for its last
    eight seconds: 0.85 of 30s is 25.5s, and the dead zone sat between the
    samples. A short clip still gets three looks — for six seconds that is the
    whole video — but anything longer is sampled every couple of seconds, and
    the last look sits near the end, where dead air usually is."""
    from basivo_orch.flows.nodes.video import MAX_PROBE_POINTS, probe_moments

    short = probe_moments(6)
    assert len(short) == 3

    long = probe_moments(30)
    assert len(long) >= 10
    gaps = [b - a for a, b in zip(long, long[1:], strict=False)]
    assert max(gaps) <= 2.5, "a gap wider than this is where a dead zone hides"
    assert long[-1] >= 30 * 0.95, "the tail is the most common dead zone"
    assert long[0] > 0, "time zero is before any entrance animation has played"

    # Bounded: each point is a JS evaluation, cheap but not free.
    assert len(probe_moments(600)) <= MAX_PROBE_POINTS + 1


def test_a_composition_that_brings_its_own_audio_has_it_replaced():
    """The failure this exists for: told "a voice is already recorded", an agent
    added <audio src="voice.mp3">. There is no voice.mp3, and the renderer
    treats a missing media source as a correctness error and produces nothing —
    one hallucinated filename, no video at all."""
    from basivo_orch.flows.nodes.video import inject_narration, strip_audio

    rogue = (
        '<html><body><div id="stage" data-composition-id="p" data-duration="4">'
        '<audio id="audio" src="voice.mp3" preload="auto"></audio>'
        '<div class="clip">hi</div></div>'
        "<script>window.__timelines={p:1}</script></body></html>"
    )

    stripped, count = strip_audio(rogue)
    assert count == 1 and "voice.mp3" not in stripped

    html, _, dropped = inject_narration(
        rogue,
        audio_name="narration.wav",
        audio_seconds=2.0,
        lines=[{"start": 0.1, "end": 1.0, "words": _words(("hi", 0.1, 0.5))}],
        width=640,
        height=360,
    )
    assert dropped == 1
    assert "voice.mp3" not in html, "a reference to a missing file blocks the whole render"
    assert html.count("<audio") == 1 and 'src="narration.wav"' in html


def test_self_closed_and_uppercase_audio_tags_are_caught_too():
    """Models write markup in whatever style they feel like."""
    from basivo_orch.flows.nodes.video import strip_audio

    stripped, count = strip_audio('<AUDIO SRC="a.mp3"/><audio src="b.wav" ></audio><p>keep</p>')
    assert count == 2
    assert "a.mp3" not in stripped and "b.wav" not in stripped
    assert "<p>keep</p>" in stripped


async def test_the_probe_sees_through_an_invisible_ancestor():
    """The hole that shipped a blank video.

    `opacity` does not inherit as a computed value: a heading inside a `.clip`
    at opacity 0 still computes to opacity 1. Reading only the element's own
    style, the probe reported "SCENE ONE" as visible while the renderer
    produced thirty seconds of black — and the review passed it.
    """
    from basivo_orch.flows.nodes.video import probe_composition, review

    html = """<!doctype html><html><head><style>
    #stage{width:640px;height:360px;position:relative;overflow:hidden;background:#000}
    .clip{position:absolute;inset:0;opacity:0}
    h1{color:#fff;font-size:60px}
    </style></head><body>
    <div id="stage" data-composition-id="p" data-start="0" data-duration="4"
         data-width="640" data-height="360" data-fps="24">
      <div class="clip" data-start="0" data-duration="4" data-track-index="0">
        <h1>SCENE ONE</h1>
      </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>
    <script>const tl=gsap.timeline({paused:true});tl.set('#stage',{opacity:1},0);
    window.__timelines={p:tl};</script></body></html>"""

    visible, errors = await probe_composition(html, width=640, height=360, duration=4.0)
    assert errors == []
    assert all(not text for text in visible.values()), "an opacity-0 clip hides its text"
    assert any("nothing at all is visible" in problem for problem in review(html, visible, errors))
