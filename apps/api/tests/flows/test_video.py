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
    AiVideoConfig,
    _json_object_of,
    _narrate,
    _normalise_storyboard,
    caption_lines,
    composition_code_of,
    frame_difference,
    frame_spread,
    probe_frames,
    review_frames,
    scene_problems,
    storyboard_probe_frames,
    storyboard_scene_problems,
    strip_code_fences,
)

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


def test_reasoning_before_a_fenced_composition_is_discarded():
    reply = f"I will reason first.\n```tsx\n{GOOD_SCENE}\n```\nThat is the file."
    assert composition_code_of(reply).startswith('import React from "react"')
    assert "I will reason" not in composition_code_of(reply)


def test_storyboard_json_can_follow_a_model_preface():
    parsed = _json_object_of('Here is the plan:\n{"title":"Launch","scenes":[]}\nDone')
    assert parsed["title"] == "Launch"


def test_storyboard_weights_become_an_exact_timeline():
    plan = _normalise_storyboard(
        {
            "title": "Launch",
            "scenes": [
                {"headline": "First", "duration_weight": 1},
                {"headline": "Second", "duration_weight": 3},
            ],
        },
        duration=8,
        complexity="balanced",
    )
    assert plan["scenes"][0]["start_seconds"] == 0
    assert plan["scenes"][0]["end_seconds"] == 2
    assert plan["scenes"][1]["end_seconds"] == 8


def test_storyboard_checks_copy_and_supplied_images():
    storyboard = {
        "scenes": [
            {"headline": "Build once", "supporting_text": "Ship everywhere"},
            {"headline": "Stay in control", "supporting_text": ""},
        ]
    }
    problems = storyboard_scene_problems(GOOD_SCENE, storyboard=storyboard, assets={"p0.png"})
    assert any("p0.png" in problem for problem in problems)
    assert any("planned text" in problem for problem in problems)


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


def test_complex_storyboards_are_sampled_inside_each_scene():
    storyboard = {
        "scenes": [
            {"start_seconds": 0, "end_seconds": 2},
            {"start_seconds": 2, "end_seconds": 5},
            {"start_seconds": 5, "end_seconds": 8},
        ]
    }
    frames = storyboard_probe_frames(storyboard, duration=8, fps=10)
    assert 10 in frames
    assert 35 in frames
    assert 65 in frames


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


def test_a_video_longer_than_the_limit_is_refused():
    """The cap is here rather than discovered when a worker runs out of memory
    part way through an encode."""
    with pytest.raises(ValueError):
        AiVideoConfig(brief="Launch the product", duration_seconds=MAX_DURATION_SECONDS + 1)


def test_generated_videos_default_to_complex_with_room_for_code():
    config = AiVideoConfig(brief="Launch the product", model="a-model")
    assert config.complexity == "complex"
    assert config.max_output_tokens == 12_000


async def test_short_narration_is_expanded_before_speech(monkeypatch):
    from basivo_orch.flows.nodes import speech as speech_module
    from tests.flows.fakes import FakeChatModel, says

    replies = iter(
        [
            says("Too short"),
            says("Build faster with clear workflows that keep every team aligned today."),
        ]
    )
    prompts: list[str] = []

    def respond(messages):
        prompts.append(str(messages[-1].content))
        return next(replies)

    async def fake_speak(text, *, voice, speed):
        words = [
            {"word": word, "start": index * 0.2, "end": index * 0.2 + 0.2}
            for index, word in enumerate(text.split())
        ]
        return b"RIFFvoice", len(words) * 0.2, words

    monkeypatch.setattr(speech_module, "speak", fake_speak)
    usage = {"input_tokens": 0, "output_tokens": 0}
    script, _, _, _ = await _narrate(
        AiVideoConfig(brief="Build faster", model="model", duration_seconds=4),
        make_context(_Recorder()),
        model=FakeChatModel(respond=respond),
        brief="Build faster",
        style="",
        usage=usage,
    )

    assert len(prompts) == 2
    assert "minimum" in prompts[1]
    assert script.startswith("Build faster")
    assert usage == {"input_tokens": 20, "output_tokens": 10}


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


# ---------------------------------------------------------------------------
# Rendering for real
# ---------------------------------------------------------------------------

needs_renderer = pytest.mark.skipif(
    not is_installed(),
    reason="the Remotion project is not installed here (npm install in remotion_project)",
)


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
