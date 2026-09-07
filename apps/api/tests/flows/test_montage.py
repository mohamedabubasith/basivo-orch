"""The edit, and the composition it produces.

The planning half is pure and tested here directly. Whether the composition
actually renders is proved by `test_a_montage_really_renders_to_an_mp4`, which
is skipped unless the renderer is installed — the same arrangement the other
video tests use, because a real render is several minutes of CPU.
"""

from __future__ import annotations

import json
import uuid

import pytest

from basivo_orch.flows.nodes.montage import (
    ASPECTS,
    MONTAGE_SCENE,
    THEMES,
    MontageConfig,
    _photo_ids,
    _plan,
    montage_props,
)


def _renderer_installed() -> bool:
    from basivo_orch.flows.nodes.remotion import is_installed

    return is_installed()


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
    """The static checks an agent's composition must pass apply to ours too."""
    from basivo_orch.flows.nodes.video import scene_problems

    assert scene_problems(MONTAGE_SCENE, assets={f"p{i}.jpg" for i in range(4)}) == []


def test_every_photograph_becomes_a_shot_with_its_caption():
    plan = _plan(
        MontageConfig(seconds=12, plan=json.dumps({"captions": {IDS[0]: "the first look"}})),
        IDS[:4],
    )
    props = montage_props(
        names=[f"p{i}.jpg" for i in range(4)],
        plan=plan,
        theme=THEMES["classic"],
        title="Meera and Arjun",
        subtitle="December 2026",
        end_card="Thank you",
    )

    assert [shot["name"] for shot in props["shots"]] == ["p0.jpg", "p1.jpg", "p2.jpg", "p3.jpg"]
    assert props["shots"][0]["caption"] == "the first look"
    assert props["per"] == plan["per_photo"]
    assert props["title"] == "Meera and Arjun"
    assert props["endCard"] == "Thank you"


def test_captions_and_titles_are_data_not_markup():
    """A caption comes from a model, which took it from a person's message.

    With the HTML renderer this needed escaping, and getting that wrong put a
    script tag in a wedding video. React renders text as text, so the property
    to hold onto now is that the value reaches the composition unchanged and
    is never spliced into source.
    """
    plan = _plan(
        MontageConfig(
            seconds=8,
            title="<script>alert(1)</script>",
            plan=json.dumps({"captions": {IDS[0]: "R & J <3"}}),
        ),
        IDS[:2],
    )
    props = montage_props(
        names=["p0.jpg", "p1.jpg"],
        plan=plan,
        theme=THEMES["classic"],
        title=plan["title"],
        subtitle="",
        end_card="",
    )

    assert props["title"] == "<script>alert(1)</script>"
    assert props["shots"][0]["caption"] == "R & J <3"
    # The values travel as props in job.json, so nothing a person typed ever
    # becomes part of the composition's source.
    assert "<script>" not in MONTAGE_SCENE


def test_a_shot_is_held_across_the_crossfade():
    """Each photograph is drawn one fade beyond its own slot, so the next one
    appears underneath it rather than against the background."""
    assert "start - fade" in MONTAGE_SCENE and "end + fade" in MONTAGE_SCENE
    assert "DRIFTS" in MONTAGE_SCENE, "photographs must move"


def test_portrait_is_the_default_shape():
    """It is delivered on a phone and forwarded on WhatsApp."""
    assert ASPECTS["9:16"] == (1080, 1920)
    assert MontageConfig().aspect == "9:16"


@pytest.mark.slow
@pytest.mark.skipif(not _renderer_installed(), reason="the Remotion project is not installed here")
def test_a_montage_really_renders_to_an_mp4():
    """The only test that proves the composition is valid to the renderer.

    Everything above checks the props are right; this checks that a browser
    and ffmpeg agree.
    """
    import asyncio
    import io

    from PIL import Image

    from basivo_orch.flows.nodes.remotion import RenderJob, render

    photos = {}
    for index, colour in enumerate([(180, 120, 90), (90, 120, 180), (120, 180, 90)]):
        buffer = io.BytesIO()
        Image.new("RGB", (1080, 1920), colour).save(buffer, format="JPEG")
        photos[f"p{index}.jpg"] = buffer.getvalue()

    plan = _plan(MontageConfig(seconds=6, title="Test"), IDS[:3])
    props = montage_props(
        names=list(photos),
        plan=plan,
        theme=THEMES["classic"],
        title="Test",
        subtitle="",
        end_card="",
    )
    data, _ = asyncio.run(
        render(
            RenderJob(
                scene_tsx=MONTAGE_SCENE,
                width=360,
                height=640,
                fps=12,
                duration_seconds=float(plan["seconds"]),
                props=props,
                background=THEMES["classic"]["bg"],
                quality="draft",
                assets=photos,
            )
        )
    )
    assert b"ftyp" in data[:16], "not an MP4"
    assert len(data) > 20_000, f"suspiciously small: {len(data)} bytes"


# ---------------------------------------------------------------------------
# Photographs in an agent-written composition
# ---------------------------------------------------------------------------


def test_a_composition_may_only_use_the_photographs_it_was_given():
    """A missing file renders as nothing at all.

    No error, no log, just a blank where a photograph should be, found when
    someone watches the finished video. The model is told exactly which names
    exist, so asking for another is a mistake worth sending back rather than
    rendering.
    """
    from basivo_orch.flows.nodes.video import scene_problems

    available = {"p0.jpg", "p1.jpg"}
    used = _composition_using('staticFile("p0.jpg")')
    assert scene_problems(used, assets=available) == []

    invented = scene_problems(_composition_using('staticFile("couple-hero.jpg")'), assets=available)
    assert len(invented) == 1
    assert "couple-hero.jpg" in invented[0]
    assert "p0.jpg, p1.jpg" in invented[0], "the model is told what it may use instead"


def test_an_external_image_is_refused():
    """The renderer has no network for assets, so a remote URL is an empty
    frame, and a composition reaching out to one is also a way to send whatever
    is in that URL somewhere else."""
    from basivo_orch.flows.nodes.video import scene_problems

    problems = scene_problems(
        _composition_using('"https://images.example.com/couple.jpg"'), assets={"p0.jpg"}
    )
    assert problems and "no network" in problems[0]


def test_a_data_uri_is_left_alone():
    """It carries its own bytes, so it needs neither a file nor the network."""
    from basivo_orch.flows.nodes.video import scene_problems

    assert scene_problems(_composition_using('"data:image/png;base64,AAA"'), assets=set()) == []


def _composition_using(expression: str) -> str:
    """A minimal composition that passes every other check, so a test about
    one rule is not answered by a different rule failing first."""
    return (
        'import React from "react";\n'
        'import {AbsoluteFill, Img, staticFile, useCurrentFrame} from "remotion";\n'
        "export default function Scene() {\n"
        "  const frame = useCurrentFrame();\n"
        "  return <AbsoluteFill style={{opacity: frame / 30}}>"
        f"<Img src={{{expression}}} /></AbsoluteFill>;\n"
        "}\n"
    )
