"""Video, rendered with Remotion.

Most of these are fast, because most of what can go wrong is decided before
anything is rendered: what the composition is allowed to import, what it may
reach for, whether anything moves. The ones that actually render are marked
`slow` and skipped where the renderer is not installed, because the only real
proof that a composition works is a file with frames in it.

The frame tests build their images with Pillow rather than rendering them.
What is being tested there is the verdict, not the browser: a flat frame is a
flat frame whoever drew it.
"""

from __future__ import annotations

import io
import uuid

import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.remotion import RenderJob, is_installed
from basivo_orch.flows.nodes.video import (
    MAX_DURATION_SECONDS,
    VideoRenderConfig,
    VideoRenderNode,
    caption_lines,
    frame_difference,
    frame_spread,
    probe_frames,
    review_frames,
    scene_problems,
    strip_code_fences,
)
from basivo_orch.flows.nodes.video_templates import TEMPLATE_CHOICES, TEMPLATES

GOOD_SCENE = """
import React from "react";
import {AbsoluteFill, useCurrentFrame, interpolate} from "remotion";

export default function Scene({headline}) {
  const frame = useCurrentFrame();
  const enter = interpolate(frame, [0, 12], [0, 1], {extrapolateRight: "clamp"});
  return (
    <AbsoluteFill style={{opacity: enter}}>
      <h1>{headline}</h1>
    </AbsoluteFill>
  );
}
"""


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.saved: list[bytes] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass

    def data_for(self, kind: str) -> list[dict]:
        return [data for name, data in self.steps if name == kind]


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


def png(colour, *, size=(160, 90), mark: tuple[int, int, int, int] | None = None) -> bytes:
    """A picture, as bytes. `mark` paints a rectangle so the frame is not flat."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, colour)
    if mark:
        ImageDraw.Draw(image).rectangle(mark, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# What a composition may be
# ---------------------------------------------------------------------------


def test_a_good_composition_has_nothing_wrong_with_it():
    assert scene_problems(GOOD_SCENE, assets=set()) == []


def test_an_empty_composition_says_so():
    assert scene_problems("") == ["There is no composition at all."]


def test_a_composition_with_no_default_export_is_refused():
    problems = scene_problems(GOOD_SCENE.replace("export default ", ""))
    assert any("default export" in problem for problem in problems)


def test_a_composition_may_not_import_anything_else():
    scene = 'import gsap from "gsap";\n' + GOOD_SCENE
    problems = scene_problems(scene)
    assert any("'gsap'" in problem for problem in problems)


def test_a_composition_may_not_import_another_file():
    """There is one file. An import of a sibling is a bundler error later, and
    a confusing one, because the file it names never existed."""
    scene = 'import {Card} from "./Card";\n' + GOOD_SCENE
    problems = scene_problems(scene)
    assert any("only one file" in problem for problem in problems)


@pytest.mark.parametrize("element", ["<Audio", "<Video", "<OffthreadVideo"])
def test_a_composition_may_not_bring_its_own_audio_or_video(element: str):
    """Narration is a sibling of the composition. One that adds its own gets
    two voices, or names a file that does not exist and fails the render."""
    scene = GOOD_SCENE.replace("<h1>", f"{element} src={{x}} /><h1>")
    problems = scene_problems(scene)
    assert any(element in problem for problem in problems)


def test_a_composition_where_nothing_moves_is_refused():
    still = """
    import React from "react";
    import {AbsoluteFill} from "remotion";
    export default function Scene() {
      return <AbsoluteFill><h1>Static</h1></AbsoluteFill>;
    }
    """
    problems = scene_problems(still)
    assert any("Nothing in it moves" in problem for problem in problems)


def test_a_composition_may_not_reach_the_network():
    scene = GOOD_SCENE.replace("<h1>", '<img src="https://example.com/logo.png" /><h1>')
    problems = scene_problems(scene)
    assert any("no network" in problem for problem in problems)


def test_a_composition_asking_for_a_file_we_do_not_have_is_refused():
    scene = GOOD_SCENE.replace("<h1>", '<Img src={staticFile("logo.png")} /><h1>')
    problems = scene_problems(scene, assets={"p0.png"})
    assert any("'logo.png'" in problem and "p0.png" in problem for problem in problems)


def test_a_composition_asking_for_a_file_we_do_have_is_fine():
    scene = GOOD_SCENE.replace("<h1>", '<Img src={staticFile("p0.png")} /><h1>')
    assert scene_problems(scene, assets={"p0.png"}) == []


def test_markdown_fences_are_unwrapped():
    assert strip_code_fences("```tsx\nconst a = 1;\n```") == "const a = 1;"


# ---------------------------------------------------------------------------
# What the frames say
# ---------------------------------------------------------------------------


def test_a_flat_frame_scores_near_zero_and_a_drawn_one_does_not():
    assert frame_spread(png((10, 10, 20))) < 1
    assert frame_spread(png((10, 10, 20), mark=(10, 10, 90, 60))) > 10


def test_two_identical_frames_differ_by_nothing():
    assert frame_difference(png((30, 30, 30)), png((30, 30, 30))) == 0


def test_a_moved_element_registers_as_a_difference():
    first = png((10, 10, 20), mark=(0, 0, 60, 60))
    second = png((10, 10, 20), mark=(90, 20, 150, 80))
    assert frame_difference(first, second) > 5


def test_a_video_that_is_blank_all_the_way_through_is_rejected():
    frames = [png((8, 8, 12)) for _ in range(3)]
    problems = review_frames(frames, seconds=[0.5, 3.0, 5.5])
    assert any("Every frame is a flat colour" in problem for problem in problems)


def test_a_single_empty_moment_is_named():
    frames = [
        png((8, 8, 12), mark=(4, 4, 60, 40)),
        png((8, 8, 12)),
        png((8, 8, 12), mark=(80, 20, 140, 70)),
    ]
    problems = review_frames(frames, seconds=[0.5, 3.0, 5.5])
    assert any("3.0s is empty" in problem for problem in problems)


def test_a_video_where_the_picture_never_changes_is_rejected():
    """A still image with a running time is the failure nobody notices: it
    renders, it plays, and it is worthless."""
    frame = png((20, 30, 60), mark=(10, 10, 90, 60))
    problems = review_frames([frame, frame, frame], seconds=[0.5, 3.0, 5.5])
    assert any("picture never changes" in problem for problem in problems)


def test_frames_that_differ_pass():
    frames = [
        png((20, 30, 60), mark=(0, 0, 50, 50)),
        png((20, 30, 60), mark=(50, 20, 110, 70)),
        png((20, 30, 60), mark=(100, 30, 158, 88)),
    ]
    assert review_frames(frames, seconds=[0.5, 3.0, 5.5]) == []


def test_nothing_rendered_at_all_is_a_problem():
    assert review_frames([], seconds=[]) != []


def test_the_frames_looked_at_are_spread_across_the_video():
    frames = probe_frames(10, 30)
    assert len(frames) == 3
    assert frames == sorted(set(frames))
    assert 0 <= frames[0] < frames[-1] <= 10 * 30 - 1


def test_a_very_short_video_still_has_a_frame_to_look_at():
    """A video of a frame and a half is a mistake somebody will make, and it
    must not produce a negative frame number or an empty list."""
    frames = probe_frames(0.05, 30)
    assert frames
    assert min(frames) >= 0
    assert max(frames) <= 1


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_custom_video_needs_a_composition():
    with pytest.raises(ValueError, match="needs its composition"):
        VideoRenderConfig(template="custom", scene="   ")


def test_values_must_be_a_json_object():
    with pytest.raises(ValueError, match="JSON object"):
        VideoRenderConfig(props='["a", "b"]')
    with pytest.raises(ValueError, match="JSON object"):
        VideoRenderConfig(props="not json at all")


def test_values_holding_a_reference_are_checked_at_run_time_instead():
    """`{{ ... }}` is not JSON until it is filled in, so refusing it here would
    refuse the normal case: an agent writing the copy."""
    config = VideoRenderConfig(props='{"headline": "{{ nodes.writer.output.text }}"}')
    assert "headline" in config.props


def test_a_video_longer_than_the_limit_is_refused():
    with pytest.raises(ValueError):
        VideoRenderConfig(duration_seconds=MAX_DURATION_SECONDS + 1)


@pytest.mark.parametrize("name", TEMPLATE_CHOICES)
def test_every_template_would_render(name: str):
    """The static checks the renderer applies to an agent's work apply to ours
    too. A template that breaks one of them ships a broken first experience."""
    template = TEMPLATES[name]
    assets = {
        value
        for value in template.props.values()
        if isinstance(value, str) and value.endswith((".png", ".jpg"))
    }
    for item in template.props.values():
        if isinstance(item, list):
            for entry in item:
                if isinstance(entry, dict):
                    assets |= {
                        str(value)
                        for value in entry.values()
                        if isinstance(value, str) and value.endswith((".png", ".jpg"))
                    }
    assert scene_problems(template.scene, assets=assets) == []


@pytest.mark.parametrize("name", TEMPLATE_CHOICES)
def test_every_template_says_what_it_is_for(name: str):
    template = TEMPLATES[name]
    assert template.label and template.description
    assert template.duration_seconds > 0
    assert template.props


def test_the_render_node_offers_every_template():
    """The picker and the catalogue are two lists that must not drift apart:
    a template missing from the picker cannot be chosen, and a choice with no
    template fails at run time with a KeyError."""
    choices = set(VideoRenderConfig.model_fields["template"].annotation.__args__)
    assert choices == set(TEMPLATE_CHOICES) | {"custom"}


# ---------------------------------------------------------------------------
# The job handed to the renderer
# ---------------------------------------------------------------------------


def test_length_becomes_a_whole_number_of_frames():
    assert (
        RenderJob(
            scene_tsx="", width=100, height=100, fps=30, duration_seconds=2.0, props={}
        ).duration_in_frames
        == 60
    )
    assert (
        RenderJob(
            scene_tsx="", width=100, height=100, fps=30, duration_seconds=2.017, props={}
        ).duration_in_frames
        == 61
    )


def test_a_video_shorter_than_one_frame_still_has_one():
    """Zero frames is not a video, and the renderer refuses it with a message
    about the composition rather than about the length somebody typed."""
    job = RenderJob(scene_tsx="", width=100, height=100, fps=30, duration_seconds=0.0, props={})
    assert job.duration_in_frames == 1


def test_an_asset_can_only_land_beside_the_composition(tmp_path, monkeypatch):
    """A composition is written by a model. A file name is not a path."""
    from basivo_orch.flows.nodes import remotion

    modules = tmp_path / "node_modules"
    (modules / "remotion").mkdir(parents=True)
    monkeypatch.setenv(remotion.NODE_MODULES_ENV, str(modules))

    job = RenderJob(
        scene_tsx=GOOD_SCENE,
        width=100,
        height=100,
        fps=30,
        duration_seconds=1.0,
        props={},
        assets={"../../../etc/passwd": b"nope", "p0.png": b"fine"},
    )
    project = remotion._prepare(tmp_path / "work", job)

    assert (project / "public" / "passwd").exists()
    assert (project / "public" / "p0.png").exists()
    assert not (tmp_path / "work" / "etc").exists()


def test_the_job_carries_the_captions_the_audio_and_the_props(tmp_path, monkeypatch):
    import json

    from basivo_orch.flows.nodes import remotion

    modules = tmp_path / "node_modules"
    (modules / "remotion").mkdir(parents=True)
    monkeypatch.setenv(remotion.NODE_MODULES_ENV, str(modules))

    job = RenderJob(
        scene_tsx=GOOD_SCENE,
        width=1080,
        height=1920,
        fps=24,
        duration_seconds=3.0,
        props={"headline": "Hello"},
        background="#101010",
        assets={"narration.wav": b"riff"},
        audio="narration.wav",
        captions=[{"text": "hello", "from": 0.0, "to": 1.0}],
    )
    project = remotion._prepare(tmp_path / "work", job)
    written = json.loads((project / "src" / "job.json").read_text())

    assert written["durationInFrames"] == 72
    assert written["props"] == {"headline": "Hello"}
    assert written["captions"][0]["text"] == "hello"
    assert written["audio"] == "narration.wav"
    assert written["background"] == "#101010"
    assert (project / "src" / "Scene.tsx").read_text() == GOOD_SCENE


def test_a_server_without_the_renderer_says_how_to_install_it(tmp_path, monkeypatch):
    from basivo_orch.flows.nodes import remotion

    monkeypatch.setenv(remotion.NODE_MODULES_ENV, str(tmp_path / "nothing-here"))
    job = RenderJob(
        scene_tsx=GOOD_SCENE, width=100, height=100, fps=30, duration_seconds=1, props={}
    )

    with pytest.raises(NodeError, match="not installed on this server"):
        remotion._prepare(tmp_path / "work", job)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Module not found: Error: Can't resolve 'gsap'", "can be imported"),
        ("SyntaxError: Unexpected token (12:4)", "syntax error"),
        ("Error: ENOSPC: no space left on device", "ran out of disk"),
    ],
)
def test_a_renderer_failure_is_translated_into_something_actionable(raw: str, expected: str):
    """The bundler's own words are kept, but a person should not have to read
    webpack output to learn that a package is not available."""
    from basivo_orch.flows.nodes.remotion import _readable

    message = _readable(raw)
    assert expected in message
    assert raw[:20] in message


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


def test_caption_lines_break_on_sentences_before_word_count():
    words = [
        {"word": "Make", "start": 0.0, "end": 0.2},
        {"word": "it.", "start": 0.2, "end": 0.4},
        {"word": "Then", "start": 0.5, "end": 0.7},
        {"word": "ship", "start": 0.7, "end": 0.9},
        {"word": "it.", "start": 0.9, "end": 1.1},
    ]
    lines = caption_lines(words)
    assert [line["text"] for line in lines] == ["Make it.", "Then ship it."]
    assert lines[0]["from"] == 0.0
    assert lines[0]["to"] == 0.4


def test_a_long_sentence_is_split_so_a_line_fits_on_a_phone():
    words = [{"word": f"w{i}", "start": i * 0.2, "end": i * 0.2 + 0.2} for i in range(14)]
    lines = caption_lines(words)
    assert len(lines) == 3
    assert all(len(line["text"].split()) <= 6 for line in lines)


def test_caption_lines_never_overlap():
    """Two lines on screen at once is the failure people notice immediately."""
    words = [{"word": f"w{i}.", "start": i * 0.3, "end": i * 0.3 + 0.3} for i in range(6)]
    lines = caption_lines(words)
    for earlier, later in zip(lines, lines[1:], strict=False):
        assert earlier["to"] <= later["from"]


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


async def test_values_that_do_not_survive_templating_say_so():
    recorder = _Recorder()
    node = VideoRenderNode()
    config = VideoRenderConfig(props='{"headline": "{{ input.headline }}"}')
    context = make_context(recorder, input={"headline": 'a "quoted" word'})

    with pytest.raises(NodeError, match="escaping"):
        await node.run(config, context)


async def test_a_custom_composition_is_checked_before_anything_is_rendered():
    """Several minutes of work should not start on a file that cannot work."""
    recorder = _Recorder()
    node = VideoRenderNode()
    config = VideoRenderConfig(template="custom", scene="const nothing = 1;")

    with pytest.raises(NodeError, match="will not render"):
        await node.run(config, make_context(recorder))


# ---------------------------------------------------------------------------
# Rendering for real
# ---------------------------------------------------------------------------

needs_renderer = pytest.mark.skipif(
    not is_installed(),
    reason="the Remotion project is not installed here (npm install in remotion_project)",
)


@pytest.mark.slow
@needs_renderer
async def test_it_renders_a_real_video_from_a_template():
    recorder = _Recorder()
    node = VideoRenderNode()
    config = VideoRenderConfig(
        template="announcement",
        size="square",
        duration_seconds=2,
        fps=12,
        quality="draft",
        props='{"headline": "It works"}',
    )
    result = await node.run(config, make_context(recorder))

    assert result.output["format"] == "mp4"
    assert recorder.saved and len(recorder.saved[0]) > 10_000
    # An MP4 announces itself in its first bytes. A renderer that wrote a
    # zero-length file or an error page would pass a size check and fail here.
    assert recorder.saved[0][4:8] == b"ftyp"


@pytest.mark.slow
@needs_renderer
async def test_a_narrated_composition_shows_its_captions():
    """The caption layer is ours, so this is the test that it reaches the
    picture at all: a frame during the line must differ from one after it."""
    from basivo_orch.flows.nodes.remotion import probe

    job = RenderJob(
        scene_tsx=GOOD_SCENE,
        width=320,
        height=180,
        fps=12,
        duration_seconds=2.0,
        props={"headline": "Hello"},
        captions=[{"text": "a caption on screen", "from": 0.0, "to": 0.9}],
    )
    during, after = await probe(job, frames=[6, 20])
    assert frame_difference(during, after) > 1.0
