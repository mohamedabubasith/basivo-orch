# Images and video

Two nodes, one question each.

| Node | What it is for |
|---|---|
| AI Video (`video.ai`) | Describe the video. A model writes the animation, this checks it and renders an MP4. Tick **This video needs a voice** and it is narrated and captioned. |
| AI Image (`image.ai`) | Describe the picture. A model writes an HTML page, a browser screenshots it. |

There used to be seven: a template renderer, a composition renderer, a montage,
an invitation, an HTML-to-image node, a photo preparer and a speech node.
Between them they asked a person to know which one their job was before they
could start, and to wire a voice step to a video step and match the lengths by
hand. A montage is a video of photographs. An invitation is a video with names
in it. Both are now a sentence in the brief, and the photos go in the same
field. Both nodes need an LLM credential, and nothing else.

Video renders through Remotion; images render through headless Chromium,
because typography is the one thing image models still get wrong and a browser
never does.

## The renderer

[Remotion](https://www.remotion.dev). The project it renders inside lives at
`apps/api/basivo_orch/flows/nodes/remotion_project`: a package manifest, an
entry point, the root that wraps a composition, and one script that bundles,
looks at frames, and encodes in a single process.

`node_modules` is installed once into the worker image and never per render. A
render that ran `npm install` first would fail the day the registry was slow,
inside a subprocess that deliberately has no reason to reach the network.

To work on video locally:

```bash
cd apps/api/basivo_orch/flows/nodes/remotion_project
npm install
```

Anything else, point `BASIVO_REMOTION_NODE_MODULES` at an installed copy.

## Licence

Remotion is source-available, not open source. It is free for individuals and
for organisations of up to three people, and paid above that: a company licence
is $25 per seat per month for people writing compositions, or $0.01 per render
with a $100 monthly minimum for an automation, which is what this product is.

This matters twice. It applies to whoever runs this service, so a company of
four or more running Basivo needs a licence. And it applies to anyone
self-hosting this repository at that size, which is why it is written here
rather than buried in a dependency list. See
[Remotion's licence FAQ](https://www.remotion.dev/docs/license/faq).

## What an author writes

One file, exporting one component:

```tsx
import React from "react";
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate} from "remotion";

export default function Scene({headline}) {
  const frame = useCurrentFrame();
  const {fps, durationInFrames, height} = useVideoConfig();
  const enter = interpolate(frame, [0, 12], [0, 1], {extrapolateRight: "clamp"});
  return (
    <AbsoluteFill style={{justifyContent: "center", alignItems: "center"}}>
      <h1 style={{fontSize: height / 9, opacity: enter}}>{headline}</h1>
    </AbsoluteFill>
  );
}
```

The rules, each of which is checked before anything renders:

- Import from `react` and `remotion`, nothing else.
- Everything is a function of the frame. A CSS animation does not exist here:
  every frame is drawn on its own, so anything not derived from the frame is
  frozen for the whole video.
- Read sizes and timings from `useVideoConfig()`. The same composition is
  rendered landscape, square and vertical.
- No `Audio`, `Video` or captions. Those are added around the composition.
- No URLs. There is no network during a render.

## How AI Video works

AI Video uses two model stages. First it turns the brief into a timed
storyboard with exact on-screen copy, visual direction, motion, and image
assignments. It then asks the model for one complete Remotion composition from
that contract. If the model returns malformed planning JSON, the node asks it
to repair the plan once and can still continue with a deterministic fallback.

Creative complexity controls the number and density of scenes. Complex is the
default and plans up to eight scenes. The model output budget defaults to
12,000 tokens so detailed TSX is less likely to be cut off. Lower it for a
small-context model, or raise it when a provider truncates the composition.

Every planned scene contributes a frame-check timestamp. The node also checks
that planned copy and supplied images reached the generated source before it
starts the expensive final encode.

## NVIDIA NIM

Create a credential with provider **NVIDIA NIM**, paste the NVIDIA API key, and
select a model from its live catalogue. Hosted NVIDIA endpoints use
`https://integrate.api.nvidia.com/v1`. For a self-hosted NIM, set the
credential base URL to that deployment's OpenAI-compatible `/v1` endpoint.
The video node uses plain chat completions and JSON text rather than
provider-specific tool calls, so the same planning and authoring flow works
with NVIDIA and the other supported chat providers.

## What the product owns

Narration, captions and the frame count are siblings of the composition in
`src/Root.tsx`, not markup spliced into it. The previous renderer edited the
composition's HTML as a string, and every failure of that was silent: a stray
tag swallowed everything after it, and a composition that added its own
captions rendered two sets on top of each other.

That is also why the voice can no longer be cut off. The narration is spoken
first, and the video is made as long as the voice actually took.

## Looking before encoding

A composition that compiles and renders eight seconds of empty gradient is the
worst outcome there is, because nothing failed. So three small stills are
rendered from the same bundle that is about to be encoded, and two questions
are asked of them: is anything drawn, and does the picture change. A flat frame
or three identical ones send the composition back to the agent with the reason.
Stills cost about a second; a wasted render costs minutes.

## Limits

| | |
|---|---|
| Longest video | 120 seconds |
| Render timeout | 900 seconds |
| Frame check timeout | 240 seconds |
| Free disk required | 2 GB |
| Images per video | 12 |

A render checks free disk first. Filling the volume does not fail a render, it
stops Postgres accepting writes and takes the product down.

## Running the video tests

Most are fast and need no renderer. The ones that actually render are marked
`slow` and skip themselves where the project is not installed:

```bash
cd apps/api && uv run pytest tests/flows/test_video.py -q
```
