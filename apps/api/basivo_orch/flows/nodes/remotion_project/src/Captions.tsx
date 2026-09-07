/**
 * The caption layer, owned by us rather than by the composition.
 *
 * The old renderer injected caption markup into the composition's HTML with
 * string surgery, which broke in every way string surgery breaks: a stray tag
 * swallowed the markup after it, and a composition that brought its own
 * captions ended up with two sets on top of each other. Here the composition
 * is a component and the captions are a sibling, so neither can damage the
 * other, and an author who ignores the instruction not to add captions is the
 * only way to get two.
 *
 * Times are seconds, not frames: they come from a speech model's own word
 * durations, and converting once here beats converting at every call site.
 */
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";

export interface CaptionLine {
  text: string;
  from: number;
  to: number;
}

export const Captions: React.FC<{ lines: CaptionLine[] }> = ({ lines }) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const now = frame / fps;

  // The last line that has started and not yet ended. Searching backwards
  // means overlapping lines resolve to the most recent one rather than to
  // whichever happened to be written first.
  const line = [...lines].reverse().find((item) => now >= item.from && now < item.to);
  if (!line) return null;

  // Proportional to the frame, so the same component reads correctly on a
  // 1080p landscape video and a vertical story without a second set of sizes.
  const size = Math.round(height * 0.052);

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: Math.round(height * 0.09),
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          maxWidth: "82%",
          textAlign: "center",
          fontFamily:
            'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
          fontSize: size,
          lineHeight: 1.25,
          fontWeight: 700,
          color: "white",
          padding: `${Math.round(size * 0.35)}px ${Math.round(size * 0.6)}px`,
          borderRadius: Math.round(size * 0.35),
          background: "rgba(0,0,0,0.62)",
          // Captions are read over whatever the composition put behind them,
          // and a white word on a white background is not a caption.
          textShadow: "0 2px 8px rgba(0,0,0,0.55)",
        }}
      >
        {line.text}
      </div>
    </AbsoluteFill>
  );
};
