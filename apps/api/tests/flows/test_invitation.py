"""The invitation film.

The composition is a React component now, so the two halves worth testing are
the props the node builds and the properties of the component itself. Most of
these exist because the same mistake was made twice while building it: an
element hidden by one rule while a *different* element was animated. Nothing
errors, nothing logs, the ornament simply never appears, and you only find out
by watching twenty-four seconds of video. So the component is checked for the
property that was actually violated: nothing is hidden by a constant, every
opacity is driven from the frame.

The one test that renders is marked `slow` and skipped where the renderer is
not installed, because the only real proof is frames with something on them.
"""

from __future__ import annotations

import re
import uuid

import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.invitation import (
    ASPECTS,
    INVITATION_SCENE,
    PALETTES,
    Function,
    InvitationConfig,
    InvitationNode,
    invitation_props,
)

PHOTOS = ["couple0.jpg", "couple1.jpg"]


def an_invitation(**overrides) -> InvitationConfig:
    base = {
        "bride": "Meera",
        "groom": "Arjun",
        "date_line": "Sunday, 12 April 2026",
        "venue": "Sri Krishna Gardens, Coimbatore",
        "closing": "With love,\nthe Iyer and Rao families",
        "seconds": 24,
    }
    return InvitationConfig(**{**base, **overrides})


def props_for(config: InvitationConfig, photos: list[str] | None = None) -> dict:
    return invitation_props(
        config=config,
        photos=PHOTOS[:1] if photos is None else photos,
        palette=PALETTES[config.palette],
    )


# ---------------------------------------------------------------------------
# A fake run, so the job the node builds can be looked at
# ---------------------------------------------------------------------------


class _Run:
    """One run's worth of context, with the render replaced by a recorder."""

    def __init__(self, artifacts: dict[str, bytes] | None = None) -> None:
        self.artifacts = artifacts or {}
        self.steps: list[tuple[str, dict]] = []
        self.job = None

    async def _progress(self, message: str) -> None:
        pass

    async def _step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def _save(self, data: bytes, *, filename: str, content_type: str, node_id=None) -> dict:
        return {"artifact_id": "inv-1", "url": f"/artifacts/inv-1/{filename}"}

    async def _load(self, artifact_id: str) -> bytes | None:
        return self.artifacts.get(artifact_id)

    def context(self) -> NodeContext:
        return NodeContext(
            run_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            node_id="invitation",
            node_name="Wedding Invitation",
            attempt=1,
            input={},
            outputs={},
            variables={},
            trigger={},
            progress=self._progress,
            step=self._step,
            resolve_credential=None,
            http=None,
            save_artifact=self._save,
            load_artifact=self._load,
        )

    def install(self, monkeypatch) -> None:
        from basivo_orch.flows.nodes import remotion

        async def fake_render(job):
            self.job = job
            return b"\x00\x00\x00\x18ftypisom", {"durationInFrames": job.duration_in_frames}

        monkeypatch.setattr(remotion, "render", fake_render)


async def run_invitation(config: InvitationConfig, run: _Run, monkeypatch):
    run.install(monkeypatch)
    result = await InvitationNode().run(config, run.context())
    assert run.job is not None, "the node never asked for a render"
    return result


# ---------------------------------------------------------------------------
# The component
# ---------------------------------------------------------------------------


def test_the_renderer_will_accept_the_composition():
    from basivo_orch.flows.nodes.video import scene_problems

    assert scene_problems(INVITATION_SCENE, assets=set(PHOTOS)) == []


def test_nothing_is_hidden_by_a_constant():
    """The bug this suite exists for.

    Twice an ornament was hidden by one rule and animated by another, and the
    only symptom was an invitation with a blank space where the mandala should
    be. With a component there is no stylesheet to disagree with: every opacity
    that can reach zero is computed from the frame, so whatever goes out comes
    back.
    """
    values = re.findall(r"opacity: ([^,\n]+)", INVITATION_SCENE)
    assert values, "no opacity is set at all, which cannot be right"
    for value in values:
        assert "interpolate" in value or float(value) > 0, f"hidden and never revealed: {value}"


def test_the_corner_flourishes_arrive_on_the_closing_card():
    """The flourish people remember, and the reason the corners exist."""
    assert "<Corners" in INVITATION_SCENE
    assert "mark={marks.close}" in INVITATION_SCENE
    # Staggered off their own start rather than all four at once.
    assert "index * fps * 0.12" in INVITATION_SCENE


def test_every_size_comes_from_the_frame_so_all_four_shapes_work():
    assert "useVideoConfig()" in INVITATION_SCENE
    assert "Math.min(height, width * 1.4) / 100" in INVITATION_SCENE
    # Nothing is measured in pixels the component decided for itself.
    assert "1080" not in INVITATION_SCENE and "1920" not in INVITATION_SCENE


def test_the_timing_comes_from_the_running_time():
    """A twelve second cut and a thirty second cut are the same film at
    different paces, which only holds if every mark is a fraction of the
    duration the renderer was given."""
    assert "marksFor(durationInFrames" in INVITATION_SCENE
    assert "Math.round(total * weights[key])" in INVITATION_SCENE
    # The rounding remainder goes to the closing card rather than to a gap.
    assert "marks.close[1] += total - cursor" in INVITATION_SCENE


def test_something_moves_at_every_moment():
    """A scene that has settled is still: the card breathes underneath it, so
    no frame is a copy of the one before."""
    assert "const breath = interpolate(frame, [0, durationInFrames]" in INVITATION_SCENE
    assert "scale(${breath})" in INVITATION_SCENE


def test_the_composition_never_brings_its_own_sound_or_a_url():
    """Music is the render job's audio track. A composition that adds its own
    gets two, or a missing file and no video at all."""
    assert "<Audio" not in INVITATION_SCENE
    assert "http://" not in INVITATION_SCENE and "https://" not in INVITATION_SCENE
    assert "music.mp3" not in INVITATION_SCENE


# ---------------------------------------------------------------------------
# The props
# ---------------------------------------------------------------------------


def test_a_name_in_tamil_or_devanagari_survives_intact():
    """The font stack is what stops these becoming empty boxes, and the stack
    only helps if the characters reach the composition."""
    props = props_for(an_invitation(bride="மீரா", groom="अर्जुन", header_symbol="॥ शुभ विवाह ॥"))

    assert props["bride"] == "மீரா" and props["groom"] == "अर्जुन"
    assert props["symbol"] == "॥ शुभ विवाह ॥"
    assert "Noto Serif Tamil" in props["font"] and "Noto Serif Devanagari" in props["font"]


def test_a_line_break_is_a_line_break_and_markup_is_data():
    """An operator writing two lines means two lines, and this text arrives
    from a Telegram message, so nothing in it may become markup. With the HTML
    renderer that needed escaping; React renders a prop as a text node, so what
    matters now is that the value travels unchanged and is never spliced into
    the component's source.
    """
    props = props_for(an_invitation(closing="With love,\nthe families"))
    assert props["closing"] == "With love,\nthe families"
    assert 'whiteSpace: "pre-line"' in INVITATION_SCENE, "the break has to survive layout too"

    hostile = props_for(an_invitation(closing="<script>alert(1)</script>"))
    assert hostile["closing"] == "<script>alert(1)</script>"
    assert "<script>" not in INVITATION_SCENE


def test_the_photograph_is_not_hidden_behind_the_type():
    """A photography studio is selling the photograph. The first version washed
    it out under full-frame text, which is worse than having no picture."""
    assert 'height: "64%"' in INVITATION_SCENE, "the picture keeps the upper two thirds"
    # The middle of the frame, where the faces are, is left alone.
    assert "transparent 20%" in INVITATION_SCENE and "transparent 46%" in INVITATION_SCENE
    # The type moves below the picture instead of sitting on it.
    assert 'overlaid ? "flex-end" : "center"' in INVITATION_SCENE


def test_with_no_photographs_the_type_is_centred():
    props = props_for(an_invitation(), photos=[])
    assert props["photos"] == []
    assert "const overlaid = photos.length > 0" in INVITATION_SCENE


def test_each_photograph_takes_its_own_scene():
    """Left showing, the first one sits under the schedule as a smudge."""
    props = props_for(an_invitation(), photos=PHOTOS)
    assert props["photos"] == PHOTOS
    # One photograph per scene: the names, then the date and venue.
    assert "index === 0" in INVITATION_SCENE
    assert "marks.names[0] - fps * 0.5" in INVITATION_SCENE
    assert "marks.when[0] - fps * 0.4" in INVITATION_SCENE
    # And each is drawn only inside its own window.
    assert "if (frame < start || frame > start + length) return null" in INVITATION_SCENE


def test_the_photographs_move_slowly_rather_than_sitting_still():
    assert "DRIFTS" in INVITATION_SCENE
    assert "scale(${scale}) translateY(${shift}%)" in INVITATION_SCENE


def test_the_schedule_reaches_the_composition_in_order_and_is_staggered():
    config = an_invitation(
        functions=[
            Function(name="Mehendi", when="10 April", where="At home"),
            Function(name="Sangeet", when="11 April"),
            Function(name="Muhurtham", when="12 April"),
        ]
    )
    props = props_for(config)

    assert [item["name"] for item in props["functions"]] == ["Mehendi", "Sangeet", "Muhurtham"]
    assert props["functions"][0]["where"] == "At home"
    # A schedule that appears all at once is a wall, so each row waits for the
    # one before it.
    assert "index * step" in INVITATION_SCENE


def test_the_card_carries_every_fact_a_printed_one_would():
    props = props_for(an_invitation(time_line="Muhurtham at 9.30 am", joiner="weds"))
    for key in ("inviteLine", "bride", "groom", "joiner", "dateLine", "timeLine", "venue"):
        assert props[key], key
    assert props["palette"] == PALETTES["maroon_gold"]


def test_every_palette_is_a_complete_set():
    """A missing colour renders as the string 'undefined' in a gradient, which
    silently produces a black frame."""
    for name, palette in PALETTES.items():
        assert set(palette) == {"bg", "deep", "ink", "gold"}, name
        for key, value in palette.items():
            assert re.fullmatch(r"#[0-9a-f]{6}", value), f"{name}.{key} = {value!r}"


# ---------------------------------------------------------------------------
# The job the node builds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("aspect", sorted(ASPECTS))
async def test_the_job_is_the_length_and_shape_asked_for(aspect, monkeypatch):
    run = _Run()
    config = an_invitation(aspect=aspect, seconds=14, fps=24, palette="royal_blue", quality="draft")
    result = await run_invitation(config, run, monkeypatch)

    width, height = ASPECTS[aspect]
    assert (run.job.width, run.job.height) == (width, height)
    assert run.job.duration_seconds == 14
    assert run.job.fps == 24
    assert run.job.duration_in_frames == 14 * 24
    assert run.job.quality == "draft"
    # The background is the palette's own, or the first frame flashes navy.
    assert run.job.background == PALETTES["royal_blue"]["bg"]
    assert result.output["width"] == width and result.output["seconds"] == 14


async def test_the_photographs_become_assets_and_the_music_becomes_the_audio(monkeypatch):
    photo_ids = [str(uuid.uuid4()) for _ in range(2)]
    music_id = str(uuid.uuid4())
    run = _Run(
        {photo_ids[0]: b"one", photo_ids[1]: b"two", music_id: b"song"},
    )
    config = an_invitation(photos=", ".join(photo_ids), music_artifact_id=music_id)
    await run_invitation(config, run, monkeypatch)

    assert set(run.job.assets) == {"couple0.jpg", "couple1.jpg", "music.mp3"}
    # Music is the job's audio track, never something the composition knows
    # about, and never one of the photographs.
    assert run.job.audio == "music.mp3"
    assert run.job.props["photos"] == ["couple0.jpg", "couple1.jpg"]


async def test_music_that_has_expired_costs_the_soundtrack_not_the_film(monkeypatch):
    run = _Run()
    config = an_invitation(music_artifact_id=str(uuid.uuid4()))
    await run_invitation(config, run, monkeypatch)

    assert run.job.audio == ""
    assert run.job.assets == {}


async def test_details_from_an_agent_reach_the_card(monkeypatch):
    """The whole point of the JSON field: an agent read the operator's message
    and filled the card in."""
    run = _Run()
    config = an_invitation(
        bride="",
        groom="",
        details='{"bride": "Meera", "groom": "Arjun", '
        '"functions": [{"name": "Sangeet", "when": "11 April"}]}',
    )
    await run_invitation(config, run, monkeypatch)

    assert run.job.props["bride"] == "Meera"
    assert run.job.props["functions"][0]["name"] == "Sangeet"


async def test_an_invitation_needs_a_name():
    run = _Run()
    with pytest.raises(NodeError, match="at least one name"):
        await InvitationNode().run(InvitationConfig(seconds=10), run.context())


# ---------------------------------------------------------------------------
# The one that renders
# ---------------------------------------------------------------------------


def _renderer_installed() -> bool:
    from basivo_orch.flows.nodes.remotion import is_installed

    return is_installed()


@pytest.mark.slow
@pytest.mark.skipif(not _renderer_installed(), reason="the Remotion project is not installed here")
def test_an_invitation_really_renders_something_worth_watching():
    """The only test that proves the composition works.

    Everything above checks what is handed to the renderer; this checks what
    comes back out of it: three frames spread across the film, none of them a
    flat colour and none of them the same picture as the next.
    """
    import asyncio
    import io

    from PIL import Image

    from basivo_orch.flows.nodes.remotion import RenderJob, probe
    from basivo_orch.flows.nodes.video import probe_frames, review_frames

    buffer = io.BytesIO()
    Image.new("RGB", (600, 800), (150, 90, 70)).save(buffer, "JPEG")

    seconds, fps = 12.0, 24
    config = an_invitation(
        seconds=seconds,
        bride="மீரா",
        groom="अर्जुन",
        functions=[Function(name="Mehendi", when="10 April")],
    )
    job = RenderJob(
        scene_tsx=INVITATION_SCENE,
        width=540,
        height=960,
        fps=fps,
        duration_seconds=seconds,
        props=props_for(config),
        background=PALETTES[config.palette]["bg"],
        quality="draft",
        assets={"couple0.jpg": buffer.getvalue()},
    )
    frames = probe_frames(seconds, fps)
    images = asyncio.run(probe(job, frames=frames))

    assert len(images) == len(frames)
    assert review_frames(images, seconds=[frame / fps for frame in frames]) == []
