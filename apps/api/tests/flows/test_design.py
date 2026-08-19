"""Rendering a poster.

One test here actually starts a browser and produces a real PNG, because the
whole promise of this node is that the text on the poster is the text you
typed — and only a real render can show that. It is marked `slow` so a quick
loop can skip it; CI runs it.
"""

from __future__ import annotations

import uuid

import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.design import SIZES, RenderConfig, RenderNode


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


def make_context(recorder: _Recorder) -> NodeContext:
    async def save_artifact(data: bytes, *, filename: str, content_type: str, node_id=None):
        recorder.saved.append(data)
        return {
            "artifact_id": "art-1",
            "url": "/api/v1/orgs/o/artifacts/art-1",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        }

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="poster",
        node_name="Poster",
        attempt=1,
        input={"headline": "Ship the fix before standup"},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=None,  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        save_artifact=save_artifact,
    )


def test_the_channel_presets_are_the_sizes_those_channels_actually_use():
    assert SIZES["instagram_square"] == (1080, 1080)
    assert SIZES["story"] == (1080, 1920)
    assert SIZES["linkedin"] == (1200, 627)


def test_a_custom_size_needs_both_dimensions():
    with pytest.raises(ValueError, match="width and height"):
        RenderConfig(html="<p>x</p>", size="custom")
    assert RenderConfig(html="<p>x</p>", size="custom", width=800, height=400).dimensions() == (
        800,
        400,
    )


@pytest.mark.slow
async def test_it_renders_a_real_png_containing_the_text_it_was_given():
    """The point of the node: the words on the poster are the words you typed.

    An image model gets this wrong roughly one time in ten and never says so.
    A browser gets it right every time, which is the entire argument for
    rendering rather than generating.
    """
    recorder = _Recorder()
    ctx = make_context(recorder)

    result = await RenderNode().run(
        RenderConfig(
            html=(
                "<html><body style='margin:0;width:600px;height:400px;background:#111;"
                "color:#fff;font-family:sans-serif;display:flex;align-items:center;"
                "justify-content:center'><h1>{{ input.headline }}</h1></body></html>"
            ),
            size="custom",
            width=600,
            height=400,
            scale=1,
            wait_for_fonts=False,
        ),
        ctx,
    )

    image = recorder.saved[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n"), "that is not a PNG"
    assert len(image) > 2000, "suspiciously small for a rendered poster"
    assert result.output["width"] == 600
    assert result.output["content_type"] == "image/png"

    started = recorder.data_for("render.started")[0]
    assert started["width"] == 600 and started["scale"] == 1


@pytest.mark.slow
async def test_a_page_may_not_fetch_from_hosts_that_are_not_allowed():
    """The HTML can come from a model working off an untrusted brief, so the
    page must not be able to reach whatever it likes — an image tag pointed at
    a metadata endpoint would otherwise be an SSRF with a picture frame."""
    recorder = _Recorder()
    ctx = make_context(recorder)

    # The blocked request must not fail the render: the poster still comes
    # out, minus the image it was not allowed to load.
    await RenderNode().run(
        RenderConfig(
            html=(
                "<html><body style='margin:0;width:400px;height:200px;background:#fff'>"
                "<img src='http://169.254.169.254/latest/meta-data/' alt=''>"
                "<p>rendered anyway</p></body></html>"
            ),
            size="custom",
            width=400,
            height=200,
            scale=1,
            wait_for_fonts=False,
        ),
        ctx,
    )

    assert recorder.saved, "the render was abandoned instead of dropping the blocked resource"


async def test_a_render_without_somewhere_to_save_fails_clearly():
    recorder = _Recorder()
    ctx = make_context(recorder)
    object.__setattr__(ctx, "save_artifact", None)
    with pytest.raises((NodeError, TypeError)):
        await RenderNode().run(
            RenderConfig(html="<p>x</p>", size="custom", width=100, height=100, scale=1), ctx
        )
