"""Voice-over: text in, narration out, with word-level timings.

The video nodes render silent MP4s, and a silent 30-second product video is
half a product video. This is the other half.

**Local, not a cloud voice.** Kokoro-82M is Apache-2.0, runs on CPU at better
than real time, and ships 54 voices across 8 languages, so narration costs
nothing per run and needs no third-party key — the same standard the social
nodes are held to. A hosted voice can still be added later behind the same
config; nothing here assumes otherwise.

**Word timings come from the model, not from a guess.** The timestamped ONNX
export has a second output — per-phoneme frame durations — so the exact moment
each word is spoken is known. That is what makes captions honest: they land on
the word rather than on an estimate of where the word probably is. Most social
video is watched muted, so this is not a nicety.

Two operational notes that matter more than they look:

**Inference runs in a thread.** It is CPU-bound and takes seconds; on the event
loop it would stall the worker's heartbeat, and a run whose heartbeat stops is
a run the reaper takes away from us mid-render. `asyncio.to_thread` keeps the
loop free.

**The model is loaded once per process.** 82MB of ONNX per node execution would
dominate the cost of speaking one sentence.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from basivo_orch.flows.nodes.base import Node, NodeContext, NodeError, NodeResult
from basivo_orch.flows.templating import render_value

#: Where the ONNX model and the voice styles live. Baked into the worker image
#: rather than downloaded at run time: a render must not fail because a model
#: host is having a bad afternoon.
MODEL_DIR = Path(os.environ.get("BASIVO_SPEECH_MODEL_DIR", "~/.cache/basivo/speech")).expanduser()
MODEL_FILE = os.environ.get("BASIVO_KOKORO_MODEL", "kokoro-timestamped-q8f16.onnx")
VOICES_FILE = os.environ.get("BASIVO_KOKORO_VOICES", "voices-v1.0.bin")

SAMPLE_RATE = 24_000

#: Conversational narration, measured rather than assumed: the probe sentences
#: in `tests/flows/test_speech.py` come out between 2.3 and 2.8 words a second.
#: This is the number that turns "make a 30 second video" into a word budget.
WORDS_PER_SECOND = 2.5

#: A curated subset for the picker. Any name the voices file knows is accepted
#: — validated at run time — but 54 undifferentiated ids is not a choice a
#: person can make, so these are the ones with labels.
VOICES: tuple[tuple[str, str], ...] = (
    ("af_heart", "Heart — warm US female"),
    ("af_bella", "Bella — bright US female"),
    ("af_nicole", "Nicole — soft US female, close-mic"),
    ("af_nova", "Nova — clear US female"),
    ("am_michael", "Michael — steady US male"),
    ("am_puck", "Puck — lively US male"),
    ("am_onyx", "Onyx — deep US male"),
    ("bf_emma", "Emma — UK female"),
    ("bf_isabella", "Isabella — UK female, measured"),
    ("bm_george", "George — UK male"),
    ("bm_lewis", "Lewis — UK male, low"),
    ("ef_dora", "Dora — Spanish female"),
    ("ff_siwis", "Siwis — French female"),
    ("hf_alpha", "Alpha — Hindi female"),
    ("if_sara", "Sara — Italian female"),
    ("jf_alpha", "Alpha — Japanese female"),
    ("zf_xiaoxiao", "Xiaoxiao — Mandarin female"),
)

#: Which espeak language each voice prefix speaks. Getting this wrong is not a
#: subtle failure — an English phonemizer reading Hindi produces confident
#: nonsense — so it is derived from the voice rather than left to the user.
_LANGUAGE_BY_PREFIX = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}

_engine: Any = None
_engine_lock = threading.Lock()


def language_for(voice: str) -> str:
    return _LANGUAGE_BY_PREFIX.get(voice[:1], "en-us")


def model_paths() -> tuple[Path, Path]:
    return MODEL_DIR / MODEL_FILE, MODEL_DIR / VOICES_FILE


def load_engine() -> Any:
    """The process-wide Kokoro session. Built once, under a lock."""
    global _engine
    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:  # another thread won the race
            return _engine

        model, voices = model_paths()
        if not model.exists() or not voices.exists():
            raise NodeError(
                f"The voice model is not installed. Expected {model} and {voices}. "
                "Fetch them once with `make speech-model` (or set "
                "BASIVO_SPEECH_MODEL_DIR to where they already are)."
            )

        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:  # pragma: no cover - a packaging failure
            raise NodeError(f"The speech engine is not installed: {exc}") from exc

        engine = Kokoro(str(model), str(voices))

        # Upstream decides "this model reports timings" by looking for an output
        # literally named `duration`; the timestamped export calls it
        # `durations`. Without this the model would speak but report nothing,
        # and captions would silently become impossible. The name is checked
        # rather than assumed, so a future export with a different second
        # output is not misread as durations.
        names = [output.name for output in engine.sess.get_outputs()]
        if not engine.has_timings and len(names) > 1 and names[1].rstrip("s") == "duration":
            engine.has_timings = True

        _engine = engine
        return _engine


def word_timings(text: str, phonemes: list[Any]) -> list[dict[str, Any]]:
    """When each word is spoken, from the model's phoneme durations.

    Phonemes arrive in reading order with a space phoneme between words, so
    grouping on the spaces gives one span per spoken word.

    When the counts disagree — the phonemizer expands "30" into "thirty" and
    "Dr." into "doctor", so they sometimes do — the spans are distributed
    across the words by length instead. Approximate captions that stay in sync
    beat exact captions that drift a word behind for the rest of the video.
    """
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    if not words:
        return []

    groups: list[tuple[float, float]] = []
    start: float | None = None
    end: float | None = None
    for entry in phonemes:
        if entry.phoneme.strip() == "":
            if start is not None and end is not None:
                groups.append((start, end))
            start = end = None
            continue
        if start is None:
            start = entry.start
        end = entry.end
    if start is not None and end is not None:
        groups.append((start, end))

    if len(groups) == len(words):
        return [
            {"word": word, "start": round(begin, 3), "end": round(finish, 3)}
            for word, (begin, finish) in zip(words, groups, strict=True)
        ]

    # Fall back: spread the whole span over the words by character count.
    total = groups[-1][1] if groups else 0.0
    if total <= 0:
        return []
    characters = sum(len(word) for word in words) or 1
    spans: list[dict[str, Any]] = []
    cursor = groups[0][0] if groups else 0.0
    for word in words:
        share = (total - cursor) * (len(word) / characters) if characters else 0.0
        spans.append({"word": word, "start": round(cursor, 3), "end": round(cursor + share, 3)})
        cursor += share
        characters -= len(word)
    return spans


async def speak(
    text: str, *, voice: str, speed: float
) -> tuple[bytes, float, list[dict[str, Any]]]:
    """Synthesize to WAV bytes. Returns (wav, seconds, word timings)."""
    engine = await asyncio.to_thread(load_engine)

    known = engine.get_voices()
    if voice not in known:
        raise NodeError(
            f"No voice called {voice!r}. This model ships {len(known)}, including: "
            + ", ".join(known[:8])
            + "."
        )

    def synthesize():
        return engine.create_timed(text, voice=voice, speed=speed, lang=language_for(voice))

    samples, rate, phonemes = await asyncio.to_thread(synthesize)
    wav = await asyncio.to_thread(_wav_bytes, samples, rate)
    return wav, round(len(samples) / rate, 3), word_timings(text, list(phonemes))


def _wav_bytes(samples: Any, rate: int) -> bytes:
    import io

    import soundfile as sf

    buffer = io.BytesIO()
    # 16-bit PCM rather than float: half the size, and every player and every
    # ffmpeg filter graph handles it without conversion.
    sf.write(buffer, samples, rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def word_budget(seconds: float) -> int:
    """How many words fit in a duration, at conversational pace."""
    return max(1, int(seconds * WORDS_PER_SECOND))


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


class SpeakConfig(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(
        default="{{ input.text }}",
        max_length=6000,
        description="What to say. Supports {{ references }}.",
    )
    voice: str = Field(default="af_heart", max_length=40)
    #: Below 0.7 the prosody smears and above 1.4 it starts to sound anxious;
    #: the useful range for narration is 0.9 to 1.15.
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    format: Literal["wav", "mp3"] = "mp3"
    filename: str = Field(default="narration", max_length=80)


class SpeakNode(Node):
    type = "audio.speak"
    label = "Speak"
    description = (
        "Turn text into narration with a real voice, on this machine — no API "
        "key, no per-word cost. Reports when each word is spoken, so captions "
        "can land on the word."
    )
    tier = 2
    category = "design"
    config_model = SpeakConfig
    output_paths = (
        "artifact_id",
        "url",
        "duration_seconds",
        "word_count",
        "words",
        "format",
    )

    #: Synthesis is a pure function of (text, voice, speed) and touches nothing
    #: outside this system, so repeating it after a recovery is safe.
    replay_safe: ClassVar[bool] = True
    max_attempts = 2
    timeout_seconds = 300.0

    async def run(self, config: SpeakConfig, ctx: NodeContext) -> NodeResult:
        text = render_value(config.text, ctx.template_context())
        if not isinstance(text, str):
            text = str(text)
        text = text.strip()
        if not text:
            raise NodeError("There is nothing to say — the text rendered empty.")

        words = len([w for w in re.split(r"\s+", text) if w])
        await ctx.step(
            "speech.started",
            {
                "voice": config.voice,
                "speed": config.speed,
                "words": words,
                "estimated_seconds": round(words / WORDS_PER_SECOND, 1),
            },
        )
        await ctx.progress(f"Speaking {words} words as {config.voice}")

        wav, seconds, timings = await speak(text, voice=config.voice, speed=config.speed)

        audio, content_type = wav, "audio/wav"
        if config.format == "mp3":
            audio, content_type = await _to_mp3(wav), "audio/mpeg"

        saved = await ctx.save_artifact(
            audio,
            filename=f"{config.filename}.{config.format}",
            content_type=content_type,
            node_id=ctx.node_id,
        )
        await ctx.step(
            "speech.finished",
            {
                **saved,
                "duration_seconds": seconds,
                "words_per_second": round(words / seconds, 2) if seconds else None,
                "timed_words": len(timings),
            },
        )

        return NodeResult(
            output={
                **saved,
                "duration_seconds": seconds,
                "word_count": words,
                "words": timings,
                "format": config.format,
            },
            metrics={"duration_ms": int(seconds * 1000)},
        )


async def _to_mp3(wav: bytes) -> bytes:
    """WAV to MP3 through ffmpeg, which the video nodes already require.

    Worth the subprocess: 30 seconds of 24kHz mono is 1.4MB as WAV and about
    240KB as MP3, and every artifact lives in Postgres.
    """
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "96k",
        "-f",
        "mp3",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(wav)
    if process.returncode != 0 or not out:
        raise NodeError(
            "ffmpeg could not encode the narration as MP3: "
            + (err.decode(errors="replace")[:300] or "no output")
            + " — set format to wav to skip the conversion."
        )
    return out


#: Where the two files come from. Pinned URLs rather than a library's own
#: downloader: this runs in a Docker build, and a build step that reaches for
#: "whatever is latest" is a build that stops being reproducible.
MODEL_SOURCES: dict[str, str] = {
    # The *timestamped* export. The plain build has no duration output, and
    # without it captions are impossible — the voice would work and the timings
    # would silently be empty.
    MODEL_FILE: (
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped"
        "/resolve/main/onnx/model_q8f16.onnx"
    ),
    VOICES_FILE: (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin"
    ),
}


def fetch_model(*, target: Path | None = None) -> None:
    """Download the voice files. Run once, at image build time.

    Deliberately not called from the node: a render that downloads 109MB the
    first time it speaks is a render that fails the day the host is slow, and
    it would do it once per container rather than once per image.
    """
    import urllib.request

    directory = target or MODEL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_SOURCES.items():
        path = directory / name
        if path.exists() and path.stat().st_size > 1_000_000:
            print(f"have {name} ({path.stat().st_size // 1024 // 1024}MB)")
            continue
        print(f"fetching {name} …")
        if not url.startswith("https://"):  # pinned constants, checked anyway
            raise ValueError(f"Refusing to fetch a model over {url.split(':', 1)[0]}.")
        request = urllib.request.Request(  # noqa: S310 — https only, checked above
            url, headers={"user-agent": "basivo-orch"}
        )
        response = urllib.request.urlopen(request, timeout=600)  # noqa: S310
        with response, path.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        print(f"  {name}: {path.stat().st_size // 1024 // 1024}MB")
