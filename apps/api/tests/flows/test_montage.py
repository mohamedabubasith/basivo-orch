"""The edit, and the composition it produces.

The planning half is pure and tested here directly. Whether the composition
actually renders is proved by `test_a_montage_really_renders_to_an_mp4`, which
is skipped unless the renderer is installed — the same arrangement the other
video tests use, because a real render is several minutes of CPU.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest

from basivo_orch.flows.nodes.montage import (
    ASPECTS,
    CROSSFADE_SECONDS,
    THEMES,
    MontageConfig,
    _photo_ids,
    _plan,
    build_composition,
)

IDS = [str(uuid.uuid4()) for _ in range(6)]


def test_photo_ids_from_a_list_a_string_or_json():
    """A reference resolves to a real list, a person types commas, an agent
    writes JSON. All three mean the same thing."""
    assert _photo_ids(IDS) == IDS
    assert _photo_ids(", ".join(IDS)) == IDS
    assert _photo_ids(json.dumps(IDS)) == IDS
    assert _photo_ids("not-an-id, also-not") == []
    assert _photo_ids(None) == []


def test_the_default_plan_spreads_the_photographs_evenly():
    plan = _plan(MontageConfig(seconds=18), IDS)

    assert len(plan["order"]) == 6
    assert plan["per_photo"] == pytest.approx(3.0)
    assert plan["seconds"] == pytest.approx(18.0)


def test_too_many_photographs_for_the_length_are_left_out_not_flickered():
    """Thirty photographs in twenty seconds is a flicker book. It keeps the
    ones it can show properly and reports how many it dropped."""
    many = [str(uuid.uuid4()) for _ in range(30)]
    plan = _plan(MontageConfig(seconds=20), many)

    assert len(plan["order"]) == 12, "20s at a 1.6s minimum"
    assert plan["dropped"] == 18
    assert plan["per_photo"] >= 1.6


def test_a_directors_plan_is_honoured():
    plan = _plan(
        MontageConfig(
            seconds=12,
            plan=json.dumps(
                {
                    "order": [IDS[3], IDS[0]],
                    "captions": {IDS[3]: "The first look"},
                    "title": "Meera & Arjun",
                    "subtitle": "12 April 2026",
                }
            ),
        ),
        IDS,
    )

    assert plan["order"][:2] == [IDS[3], IDS[0]], "the model's order leads"
    assert plan["captions"][IDS[3]] == "The first look"
    assert plan["title"] == "Meera & Arjun"


def test_a_plan_that_forgets_photographs_still_uses_them():
    """A model that lists four of twelve has not decided the other eight should
    be thrown away."""
    plan = _plan(MontageConfig(seconds=30, plan=json.dumps({"order": IDS[:2]})), IDS)
    assert set(plan["order"]) == set(IDS), "nothing was silently dropped"
    assert plan["order"][:2] == IDS[:2]


def test_a_plan_naming_photographs_we_do_not_have_is_ignored():
    """Prompt injection, or just a hallucinated id."""
    plan = _plan(
        MontageConfig(seconds=12, plan=json.dumps({"order": ["../../etc/passwd", IDS[1]]})), IDS
    )
    assert "../../etc/passwd" not in plan["order"]
    assert plan["order"][0] == IDS[1]


def test_prose_instead_of_json_does_not_stop_the_job():
    plan = _plan(MontageConfig(seconds=12, plan="Sure! Here is a lovely plan:"), IDS)
    assert len(plan["order"]) == 6, "it fell back to the even cut"


def test_the_composition_has_what_the_renderer_requires():
    from basivo_orch.flows.nodes.video import composition_problems

    plan = _plan(MontageConfig(seconds=12, title="Meera & Arjun"), IDS[:4])
    html = build_composition(
        names=[f"p{i}.jpg" for i in range(4)],
        plan=plan,
        width=1080,
        height=1920,
        fps=30,
        theme=THEMES["classic"],
        music=True,
    )

    assert composition_problems(html) == []
    assert 'data-duration="12.0' in html
    assert 'data-fps="30"' in html
    # Photographs are referenced as files beside index.html, never inlined.
    assert 'src="p0.jpg"' in html and "base64" not in html
    assert 'data-audio-src="music.mp3"' in html


def test_captions_and_titles_are_escaped():
    """A caption comes from a model, which took it from a person's message."""
    plan = _plan(
        MontageConfig(
            seconds=8,
            title="<script>alert(1)</script>",
            plan=json.dumps({"captions": {IDS[0]: "R & J <3"}}),
        ),
        IDS[:2],
    )
    html = build_composition(
        names=["p0.jpg", "p1.jpg"],
        plan=plan,
        width=1080,
        height=1920,
        fps=30,
        theme=THEMES["classic"],
        music=False,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "R &amp; J &lt;3" in html


def test_every_shot_fades_up_and_all_but_the_last_fade_out():
    plan = _plan(MontageConfig(seconds=9), IDS[:3])
    html = build_composition(
        names=["p0.jpg", "p1.jpg", "p2.jpg"],
        plan=plan,
        width=1080,
        height=1920,
        fps=30,
        theme=THEMES["classic"],
        music=False,
    )
    assert html.count("opacity:1") >= 3
    # The last shot holds to the end rather than fading to black mid-frame.
    assert "'#s2', {opacity:0" not in html
    assert f"duration:{CROSSFADE_SECONDS:.3f}" in html


def test_portrait_is_the_default_shape():
    """It is delivered on a phone and forwarded on WhatsApp."""
    assert ASPECTS["9:16"] == (1080, 1920)
    assert MontageConfig().aspect == "9:16"


@pytest.mark.skipif(shutil.which("hyperframes") is None, reason="renderer not installed")
def test_a_montage_really_renders_to_an_mp4(tmp_path):
    """The only test that proves the composition is valid to the renderer.

    Everything above checks the HTML says the right things; this checks a
    browser and ffmpeg agree.
    """
    import asyncio
    import io

    from PIL import Image

    from basivo_orch.flows.nodes.video import VideoRenderConfig, _render

    photos = {}
    for index, colour in enumerate([(180, 120, 90), (90, 120, 180), (120, 180, 90)]):
        buffer = io.BytesIO()
        Image.new("RGB", (1080, 1920), colour).save(buffer, format="JPEG")
        photos[f"p{index}.jpg"] = buffer.getvalue()

    plan = _plan(MontageConfig(seconds=6, title="Test"), IDS[:3])
    html = build_composition(
        names=list(photos),
        plan=plan,
        width=540,
        height=960,
        fps=24,
        theme=THEMES["classic"],
        music=False,
    )

    data, logs = asyncio.run(
        _render(
            html,
            variables={},
            config=VideoRenderConfig(template="custom", html=html, fps=24, quality="draft"),
            assets=photos,
        )
    )
    assert data[4:12] == b"ftypisom" or b"ftyp" in data[:16], "not an MP4"
    assert len(data) > 20_000, f"suspiciously small: {len(data)} bytes"


# ---------------------------------------------------------------------------
# Photographs in an agent-written composition
# ---------------------------------------------------------------------------


def test_a_composition_may_only_use_the_photographs_it_was_given():
    """A browser renders a missing image as nothing at all.

    No error, no log, just a blank where a photograph should be — found when
    someone watches the finished video. The model is told exactly which names
    exist, so asking for another is a mistake worth sending back rather than
    rendering.
    """
    from basivo_orch.flows.nodes.video import missing_images

    available = {"p0.jpg", "p1.jpg"}

    assert missing_images('<img src="p0.jpg"><img src="p1.jpg">', available) == []

    invented = missing_images('<img src="couple-hero.jpg">', available)
    assert len(invented) == 1
    assert "couple-hero.jpg" in invented[0]
    assert "p0.jpg, p1.jpg" in invented[0], "the model is told what it may use instead"


def test_an_external_image_is_refused():
    """The renderer has no network for assets, so a remote URL is an empty
    frame — and a composition reaching out to one is also an exfiltration path
    for whatever ends up in the URL."""
    from basivo_orch.flows.nodes.video import missing_images

    problems = missing_images('<img src="https://images.example.com/couple.jpg">', {"p0.jpg"})
    assert problems and "no network" in problems[0]


def test_the_gsap_tag_is_not_mistaken_for_a_missing_asset():
    """It is in the shell we provide, not something the model chose."""
    from basivo_orch.flows.nodes.video import missing_images

    shell = '<script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>'
    assert missing_images(shell, set()) == []


def test_data_uris_and_anchors_are_left_alone():
    from basivo_orch.flows.nodes.video import missing_images

    assert missing_images('<img src="data:image/png;base64,AAA"><a href="#end">x</a>', set()) == []
