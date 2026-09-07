/**
 * What actually gets rendered: the composition, plus the things the product
 * owns rather than the author.
 *
 * The author writes ONE component and default-exports it from Scene.tsx. It
 * never has to think about narration, captions, or the exact frame count,
 * because getting any of those wrong is how a video ends up with the voice
 * cut off mid-word or with two sets of subtitles. Those are added here, around
 * whatever the author wrote.
 *
 * Everything variable arrives through job.json, which the renderer writes
 * before bundling. It is imported rather than passed as CLI props so the
 * numbers are settled at build time: a composition whose dimensions depend on
 * runtime props renders at the wrong size exactly often enough to be a
 * support burden.
 */
import React from "react";
import { AbsoluteFill, Audio, Composition, staticFile } from "remotion";

import job from "./job.json";
import { Captions, type CaptionLine } from "./Captions";
import Scene from "./Scene";

const Stage: React.FC<Record<string, unknown>> = (props) => {
  const captions = (job.captions ?? []) as CaptionLine[];
  return (
    <AbsoluteFill style={{ backgroundColor: job.background || "#000000" }}>
      {/* The author's work. Any error it throws surfaces as a failed render
          with a stack that names their file, not ours. */}
      <Scene {...props} />
      {captions.length > 0 ? <Captions lines={captions} /> : null}
      {/* Narration is a sibling of the scene, so a composition that forgets to
          include it still gets a voice, and one that tries to bring its own
          cannot silence this one. */}
      {job.audio ? <Audio src={staticFile(job.audio)} /> : null}
    </AbsoluteFill>
  );
};

export const Root: React.FC = () => (
  <Composition
    id="Main"
    component={Stage}
    durationInFrames={job.durationInFrames}
    fps={job.fps}
    width={job.width}
    height={job.height}
    defaultProps={job.props as Record<string, unknown>}
  />
);
