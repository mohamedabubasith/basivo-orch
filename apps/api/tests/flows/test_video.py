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
