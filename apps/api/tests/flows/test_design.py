"""AI Image: a model writes a page, a browser draws it.

Two of these actually start a browser and produce a real PNG, because the
whole promise of this node is that the words on the picture are the words the
flow asked for — and only a real render can show that. They are marked `slow`
so a quick loop can skip them; CI runs them.
"""

from __future__ import annotations

import uuid

import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.design import SIZES, AiImageConfig, AiImageNode, _html_of
from tests.flows.fakes import FakeChatModel, says

PAGE = (
    "<html><body style='margin:0;width:600px;height:400px;background:#111;color:#fff;"
    "font-family:sans-serif;display:flex;align-items:center;justify-content:center'>"
    "<h1>Ship the fix before standup</h1></body></html>"
)


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
        input={"headline": "Ship the fix before standup", "blank": ""},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=None,  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        save_artifact=save_artifact,
    )


def scripted(monkeypatch, respond) -> list[list]:
    """Install a scripted model and hand back the conversations it was sent."""
    seen: list[list] = []

    def capture(messages):
        seen.append(list(messages))
        return respond(messages)

    async def fake_build_model(ctx, **kwargs):
        return FakeChatModel(respond=capture)

    monkeypatch.setattr("basivo_orch.flows.nodes.models.build_chat_model", fake_build_model)
    return seen


def config(**overrides) -> AiImageConfig:
    settings: dict = {
        "brief": "A poster that says {{ input.headline }}",
        "size": "custom",
        "width": 600,
        "height": 400,
        "scale": 1,
        "model": "a-model",
    }
    settings.update(overrides)
    return AiImageConfig(**settings)


def test_the_channel_presets_are_the_sizes_those_channels_actually_use():
    assert SIZES["instagram_square"] == (1080, 1080)
    assert SIZES["story"] == (1080, 1920)
    assert SIZES["linkedin"] == (1200, 627)


def test_a_custom_size_needs_both_dimensions():
    with pytest.raises(ValueError, match="width and height"):
        AiImageConfig(brief="a poster", size="custom")
    assert AiImageConfig(brief="a poster", size="custom", width=800, height=400).dimensions() == (
        800,
        400,
    )


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("<html><body>hi</body></html>", "<html><body>hi</body></html>"),
        ("```html\n<html><body>hi</body></html>\n```", "<html><body>hi</body></html>"),
        ("Here you go:\n<!doctype html><html>hi</html>", "<!doctype html><html>hi</html>"),
        ("I cannot do that", ""),
    ],
)
def test_the_document_is_taken_out_of_whatever_the_model_wrapped_it_in(reply, expected):
    """A sentence before the document renders as a stray line at the top of
    the picture, which is a defect nobody attributes to the model's manners."""
    assert _html_of(reply) == expected


@pytest.mark.slow
async def test_it_renders_a_real_png_containing_the_words_it_was_briefed_with(monkeypatch):
    """The point of the node: the words on the poster are the words you asked
    for. An image model gets this wrong roughly one time in ten and never says
    so; a browser gets it right every time."""
    conversations = scripted(monkeypatch, lambda _messages: says(PAGE))
    recorder = _Recorder()

    result = await AiImageNode().run(config(), make_context(recorder))

    image = recorder.saved[0]
    assert image.startswith(b"\x89PNG\r\n\x1a\n"), "that is not a PNG"
    assert len(image) > 2000, "suspiciously small for a rendered poster"
    assert result.output["width"] == 600
    assert result.output["attempts"] == 1
    assert result.output["content_type"] == "image/png"
    # The brief reached the model with its reference filled in, not as `{{ }}`.
    asked = conversations[0][-1].content
    assert "Ship the fix before standup" in asked
    assert result.metrics["cost_usd"] >= 0


@pytest.mark.slow
async def test_a_blank_page_is_sent_back_to_be_fixed(monkeypatch):
    """A page that renders as one flat colour is the failure that costs most:
    nothing errors, and a poster of pure white is posted to a customer."""
    blank = "<html><body style='margin:0;width:600px;height:400px;background:#fff'></body></html>"
    replies = iter([says(blank), says(PAGE)])
    scripted(monkeypatch, lambda _messages: next(replies))
    recorder = _Recorder()

    result = await AiImageNode().run(config(max_attempts=2), make_context(recorder))

    assert result.output["attempts"] == 2
    rejected = recorder.data_for("image.rejected")[0]
    assert "flat colour" in rejected["problems"][0]


async def test_a_page_that_stays_blank_fails_with_something_to_do_about_it(monkeypatch):
    blank = "<html><body style='margin:0;width:20px;height:20px;background:#fff'></body></html>"
    scripted(monkeypatch, lambda _messages: says(blank))
    recorder = _Recorder()

    with pytest.raises(NodeError, match="Tries"):
        await AiImageNode().run(config(max_attempts=1, width=20, height=20), make_context(recorder))


async def test_a_brief_that_renders_empty_is_refused_before_any_model_is_called(monkeypatch):
    calls: list[int] = []
    scripted(monkeypatch, lambda _messages: calls.append(1) or says(PAGE))
    recorder = _Recorder()

    with pytest.raises(NodeError, match="rendered empty"):
        await AiImageNode().run(config(brief="{{ input.blank }}"), make_context(recorder))
    assert calls == [], "a model was paid to draw nothing"


@pytest.mark.slow
async def test_a_page_may_not_fetch_from_hosts_that_are_not_allowed(monkeypatch):
    """The page is written by a model working off a brief that may have come
    from a stranger, so it must not reach whatever it likes — an image tag
    pointed at a metadata endpoint would be an SSRF with a picture frame."""
    page = (
        "<html><body style='margin:0;width:400px;height:200px;background:#fff;color:#000'>"
        "<img src='http://169.254.169.254/latest/meta-data/' alt=''>"
        "<h1>rendered anyway</h1></body></html>"
    )
    scripted(monkeypatch, lambda _messages: says(page))
    recorder = _Recorder()

    # The blocked request must not fail the render: the poster still comes
    # out, minus the resource it was not allowed to load.
    await AiImageNode().run(config(width=400, height=200), make_context(recorder))

    assert recorder.saved, "the render was abandoned instead of dropping the blocked resource"


async def test_a_render_without_somewhere_to_save_fails_clearly(monkeypatch):
    scripted(monkeypatch, lambda _messages: says(PAGE))
    recorder = _Recorder()
    ctx = make_context(recorder)
    object.__setattr__(ctx, "save_artifact", None)

    with pytest.raises(NodeError, match="cannot save"):
        await AiImageNode().run(config(), ctx)
