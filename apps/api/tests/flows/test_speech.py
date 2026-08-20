"""Voice-over.

Split deliberately in two. The pure functions — word timings, the word budget,
the language mapping — are tested with fabricated phoneme spans, so they run in
milliseconds with no model on disk. The tests that need the real 82MB model
skip when it is absent, and they are the only place the *measured* claims live:
that narration comes out near 2.5 words a second, and that the timings the
captions depend on are actually populated.

That split matters because CI has no model and a laptop does. A suite that
needed the model would be skipped everywhere and prove nothing; one that never
used it would let the words-per-second figure — which the whole video timing
design rests on — drift without anyone noticing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest

from basivo_orch.flows.nodes.base import NodeContext, NodeError
from basivo_orch.flows.nodes.speech import (
    VOICES,
    WORDS_PER_SECOND,
    SpeakConfig,
    SpeakNode,
    language_for,
    model_paths,
    word_budget,
    word_timings,
)


@dataclass(frozen=True)
class _Phoneme:
    phoneme: str
    start: float
    end: float


def spans(*items: tuple[str, float, float]) -> list[_Phoneme]:
    return [_Phoneme(*item) for item in items]


# ---------------------------------------------------------------------------
# Word timings
# ---------------------------------------------------------------------------


def test_words_are_grouped_on_the_space_phoneme():
    """The mechanism captions depend on: one span per spoken word."""
    timings = word_timings(
        "hi there",
        spans(
            ("h", 0.0, 0.1),
            ("aɪ", 0.1, 0.3),
            (" ", 0.3, 0.35),
            ("ð", 0.35, 0.5),
            ("ɛɹ", 0.5, 0.8),
        ),
    )
    assert timings == [
        {"word": "hi", "start": 0.0, "end": 0.3},
        {"word": "there", "start": 0.35, "end": 0.8},
    ]


def test_a_word_count_mismatch_falls_back_to_spreading_them():
    """The phonemizer expands "30" into "thirty", so counts do disagree.

    Captions that drift a word behind for the rest of the video are worse than
    approximate ones that stay in sync, so the span is distributed instead.
    """
    # Three phoneme groups against two written words.
    timings = word_timings(
        "ship 30",
        spans(
            ("ʃ", 0.0, 0.2),
            (" ", 0.2, 0.25),
            ("θ", 0.25, 0.5),
            (" ", 0.5, 0.55),
            ("t", 0.55, 0.9),
        ),
    )
    assert [entry["word"] for entry in timings] == ["ship", "30"]
    assert timings[0]["start"] == 0.0
    # Still covers the whole utterance and never goes backwards.
    assert timings[-1]["end"] == pytest.approx(0.9, abs=0.01)
    assert timings[0]["end"] <= timings[1]["start"]


def test_no_words_and_no_phonemes_are_both_empty():
    assert word_timings("", spans(("h", 0.0, 0.1))) == []
    assert word_timings("hello", []) == []


def test_punctuation_stays_attached_to_its_word():
    """Captions render the written word, so the comma travels with it."""
    timings = word_timings(
        "go, now",
        spans(("g", 0.0, 0.2), (" ", 0.2, 0.3), ("n", 0.3, 0.6)),
    )
    assert [entry["word"] for entry in timings] == ["go,", "now"]


# ---------------------------------------------------------------------------
# The budget and the voices
# ---------------------------------------------------------------------------


def test_the_word_budget_is_what_makes_a_30_second_script_writable():
    assert word_budget(30) == 75
    assert word_budget(6) == 15
    # Never zero, or an agent would be told to write nothing at all.
    assert word_budget(0.1) == 1


@pytest.mark.parametrize(
    ("voice", "language"),
    [
        ("af_heart", "en-us"),
        ("bm_george", "en-gb"),
        ("ff_siwis", "fr-fr"),
        ("hf_alpha", "hi"),
        ("zf_xiaoxiao", "cmn"),
        ("unknown", "en-us"),
    ],
)
def test_the_language_comes_from_the_voice_not_the_user(voice, language):
    """An English phonemizer reading Hindi produces confident nonsense, so this
    is derived rather than configured."""
    assert language_for(voice) == language


def test_every_curated_voice_has_a_label_a_person_can_choose_from():
    ids = [voice for voice, _ in VOICES]
    assert len(ids) == len(set(ids))
    for _, label in VOICES:
        assert "—" in label, "a label has to say something beyond the id"


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, dict]] = []
        self.artifacts: list[tuple[bytes, str]] = []

    async def step(self, kind: str, data: dict) -> None:
        self.steps.append((kind, data))

    async def progress(self, message: str) -> None:
        pass

    async def save_artifact(self, data, *, filename, content_type, node_id=None):
        self.artifacts.append((data, content_type))
        return {
            "artifact_id": str(uuid.uuid4()),
            "url": f"/artifacts/{filename}",
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        }

    def data_for(self, kind: str) -> list[dict]:
        return [data for k, data in self.steps if k == kind]


def make_context(recorder: _Recorder, *, http: httpx.AsyncClient, text="Ship in minutes."):
    async def resolve_credential(_id: str):
        return None

    return NodeContext(
        run_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        node_id="speak_1",
        node_name="Speak",
        attempt=1,
        input={"text": text},
        outputs={},
        variables={},
        trigger={},
        progress=recorder.progress,
        step=recorder.step,
        resolve_credential=resolve_credential,
        http=http,
        save_artifact=recorder.save_artifact,
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


async def test_empty_text_is_refused_before_the_model_is_touched(http_client):
    """Loading 82MB of ONNX to say nothing is a slow way to fail."""
    recorder = _Recorder()
    with pytest.raises(NodeError, match="nothing to say"):
        await SpeakNode().run(
            SpeakConfig(text="{{ input.text }}"),
            make_context(recorder, http=http_client, text="   "),
        )
    assert recorder.steps == []


async def test_an_unknown_voice_names_some_real_ones(http_client, monkeypatch):
    from basivo_orch.flows.nodes import speech

    class _Engine:
        def get_voices(self):
            return ["af_heart", "am_michael", "bf_emma"]

    monkeypatch.setattr(speech, "load_engine", lambda: _Engine())
    with pytest.raises(NodeError, match="af_heart"):
        await speech.speak("hello", voice="Morgan Freeman", speed=1.0)


async def test_the_node_reports_words_and_timings_for_the_caption_layer(
    http_client, monkeypatch
):
    """What downstream nodes consume: a duration and per-word spans."""
    from basivo_orch.flows.nodes import speech

    async def fake_speak(text, *, voice, speed):
        return b"RIFFfake", 2.0, [{"word": "Ship", "start": 0.0, "end": 0.5}]

    monkeypatch.setattr(speech, "speak", fake_speak)
    recorder = _Recorder()

    result = await SpeakNode().run(
        SpeakConfig(text="{{ input.text }}", format="wav"),
        make_context(recorder, http=http_client),
    )

    assert result.output["duration_seconds"] == 2.0
    assert result.output["word_count"] == 3
    assert result.output["words"][0]["word"] == "Ship"
    assert recorder.artifacts[0][1] == "audio/wav"
    started = recorder.data_for("speech.started")[0]
    assert started["estimated_seconds"] == round(3 / WORDS_PER_SECOND, 1)
    assert recorder.data_for("speech.finished")[0]["timed_words"] == 1


async def test_a_missing_model_says_how_to_install_it(monkeypatch):
    """The failure a first deployment hits, so the message is the fix."""
    from basivo_orch.flows.nodes import speech

    monkeypatch.setattr(speech, "_engine", None)
    monkeypatch.setattr(speech, "MODEL_DIR", speech.Path("/nowhere/at/all"))
    with pytest.raises(NodeError, match="make speech-model"):
        speech.load_engine()


# ---------------------------------------------------------------------------
# With the real model. Skipped where it is not installed.
# ---------------------------------------------------------------------------

model, voices = model_paths()
needs_model = pytest.mark.skipif(
    not (model.exists() and voices.exists()),
    reason="the voice model is not installed on this machine",
)


@needs_model
async def test_real_narration_lands_near_the_documented_pace():
    """The measurement the whole video timing design rests on.

    If a voice change or a model update moved the pace, every generated script
    would start overrunning its scene, and it would look like a timing bug
    rather than a changed constant. This fails instead.
    """
    from basivo_orch.flows.nodes.speech import speak

    text = (
        "Meet Basivo. Your workflows run themselves, on a schedule you choose. "
        "Connect a repository, describe the fix, and review the pull request."
    )
    wav, seconds, timings = await speak(text, voice="af_heart", speed=1.0)

    words = len(text.split())
    pace = words / seconds
    assert 1.9 <= pace <= 3.2, f"{pace:.2f} words/second is outside the documented range"
    assert abs(pace - WORDS_PER_SECOND) < 0.8

    assert wav.startswith(b"RIFF")
    # The timings captions depend on: one per word, in order, inside the clip.
    assert len(timings) == words
    assert timings[0]["start"] < 0.6
    assert timings[-1]["end"] <= seconds + 0.05
    assert all(
        a["end"] <= b["start"] + 0.001 for a, b in zip(timings, timings[1:], strict=False)
    )


@needs_model
async def test_speed_changes_the_length_in_the_direction_you_would_expect():
    from basivo_orch.flows.nodes.speech import speak

    line = "Ship in minutes, not weeks."
    _, slow, _ = await speak(line, voice="am_michael", speed=0.9)
    _, fast, _ = await speak(line, voice="am_michael", speed=1.3)
    assert fast < slow
