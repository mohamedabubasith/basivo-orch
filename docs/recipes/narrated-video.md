# Recipe: a 30-second video that talks

The video nodes render silent MP4s. Turn on **Narration** and the same node
writes a script, speaks it, times the animation to the voice, and burns in
word-level captions.

## Why the order matters

A scene lasts five seconds. A sentence lasts however long the words take. Get
the order wrong and the voice is cut off at every scene boundary.

So the node works in this order, and it is not an implementation detail — it is
the whole reason narration works:

1. **The script first.** The agent is given a word *range* derived from the
   duration — the voice runs at about **2.5 words a second**, so 30 seconds is
   64–75 words. Over the ceiling and the video would end mid-sentence; under the
   floor and it ends in silence. Both are checked, and the agent gets one
   revision to fix either.
2. **Then the voice.** Kokoro speaks it locally — no API key, no per-word cost —
   and reports the exact second each word is spoken.
3. **Then the animation**, authored against the real length, with the spoken
   word times in the prompt: *"0.0s Work 1.2s piles 2.0s up…"*. The agent is told
   to cut on those moments. A cut that lands on the word being said is the
   difference between a video and a slideshow with sound.
4. **Then captions**, driven by the composition's own GSAP timeline, with the
   spoken word bright and the rest of the line at 45%.

If the agent still declares a duration shorter than the audio, the node widens
it and logs `video.duration_widened`. The voice is never the thing that gets
cut.

## What you configure

| Field | Notes |
|---|---|
| **Narration** | Off by default. On adds the script + voice passes. |
| **Voice** | 17 labelled voices, US/UK English plus Spanish, French, Hindi, Italian, Japanese, Mandarin. The language is derived from the voice — an English phonemizer reading Hindi produces confident nonsense. |
| **Voice speed** | 0.9–1.15 is the useful range for narration. |
| **Captions** | On by default when narrating. Most short-form video is watched muted. |

## Reading the run

```
video.script               51 words, range [64, 75]
video.spoken               20.5s, af_heart, 2.48 words/second
video.attempt              problems: ['nothing is visible at 25.5s …']
video.attempt              problems: []
video.narration_attached   20.5s, 10 caption lines, captions_rendered: true
video.finished             launch.mp4
```

The narration is saved as its own artifact as well as being mixed into the
video, so you can play the voice on the run page and judge it by ear without
downloading the MP4.

## Narration on its own

Voice-over is an option on the Describe a Video node; turn on **Add a voice-over**. The standalone Speak node (`audio.speak`) is no longer in the palette but keeps working in flows that have it: text
in, an audio artifact out, plus `words` — the per-word timings. Useful for a
Telegram voice note, an accessibility read-out, or feeding a composition you
wrote by hand (`video.render` takes the artifact).

## Installing the voice

The model is two files, about 109MB, fetched once:

```bash
make speech-model
```

The worker image should bake them in — a render that downloads 109MB the first
time it speaks is a render that fails the day the host is slow. `BASIVO_SPEECH_MODEL_DIR`
points at them if they live somewhere else.

## What this is not

No voice cloning. The models that clone from a sample are either
non-commercial (XTTS v2 is CPML) or a liability: cloning a real person's voice
without recorded consent is not something to leave switched on in a workflow
that runs unattended at 6am.
