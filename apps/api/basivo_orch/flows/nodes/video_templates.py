"""Starter compositions, so nobody meets this node with an empty editor.

Each template is one Remotion component: a `.tsx` file that default-exports
`Scene`, plus the example props a picker fills in. The renderer wraps it with
narration, captions and the exact frame count, so a template that brought its
own audio or subtitles would fight the product for them. None of these do.

Two constraints run through every scene here, and both come from a rendered
video rather than from taste:

**Nothing is sized in pixels that were typed.** Every size comes from
`useVideoConfig()`, so the same template is deliberate at 1920x1080, 1080x1080
and 1080x1920. A template with a 96px headline is a template that overflows
the moment somebody renders a vertical cut.

**Nothing is timed in frames that were typed either.** Beats are fractions of
`durationInFrames`. Asked for six seconds a scene lands its beats in six; asked
for twenty it spreads them, instead of animating for one second and then
holding a still image for nineteen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Template:
    """One starter composition, and what a picker needs to show it."""

    label: str
    description: str
    #: The .tsx source, default-exporting the component.
    scene: str
    #: Example props, which are also the defaults the picker fills in.
    props: dict
    duration_seconds: float
    background: str = "#0b1020"


_BRAND_INTRO = """
import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

// A missing file name draws nothing at all. Remotion's Img cancels the whole
// render when a source fails to load, so the failure is caught here and the
// image simply leaves the layout.
function Shot({ name, style }) {
  const [broken, setBroken] = React.useState(false);
  if (!name || broken) return null;
  return (
    <Img
      src={staticFile(name)}
      maxRetries={0}
      onError={() => setBroken(true)}
      style={style}
    />
  );
}

export default function Scene({ brand, tagline, logo }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  // One unit behind every size in the scene, so the layout holds at any
  // aspect ratio instead of being tuned to one of them.
  const u = Math.min(width, height);
  const at = (f) => durationInFrames * f;
  const seconds = frame / fps;

  // Split into words, then into letters inside each word. A flat list of
  // letters in a wrapping row breaks a name in half at the frame edge, which
  // is what "Northwind Studi / o" looked like before.
  const words = String(brand || "").split(" ");
  const stagger = Math.max(1, Math.round(durationInFrames * 0.008));

  const mark = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.28),
  });
  const tag = interpolate(frame, [at(0.3), at(0.48)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const rule = interpolate(frame, [at(0.24), at(0.7)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // A slow rise across the whole video, so no second of it is a still frame.
  const drift = interpolate(frame, [0, durationInFrames], [u * 0.02, -u * 0.02]);

  const orbs = [
    { tint: "rgba(99,102,241,0.55)", size: 1.15, speed: 0.42, phase: 0 },
    { tint: "rgba(236,72,153,0.38)", size: 0.9, speed: 0.61, phase: 2.1 },
    { tint: "rgba(56,189,248,0.34)", size: 0.75, speed: 0.83, phase: 4.4 },
  ];

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#f8fafc" }}>
      {orbs.map((orb, i) => {
        const angle = seconds * orb.speed + orb.phase;
        const size = u * orb.size;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              width: size,
              height: size,
              left: width / 2 - size / 2 + Math.cos(angle) * u * 0.24,
              top: height / 2 - size / 2 + Math.sin(angle) * u * 0.18,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${orb.tint}, rgba(0,0,0,0) 68%)`,
              filter: `blur(${u * 0.04}px)`,
            }}
          />
        );
      })}

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          padding: u * 0.09,
          transform: `translateY(${drift}px)`,
        }}
      >
        <Shot
          name={logo}
          style={{
            width: u * 0.2,
            height: u * 0.2,
            objectFit: "contain",
            marginBottom: u * 0.05,
            opacity: mark,
            transform: `scale(${0.7 + mark * 0.3}) translateY(${
              Math.sin(seconds * 1.6) * u * 0.006
            }px)`,
          }}
        />

        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            gap: `0 ${u * 0.028}px`,
            fontSize: u * 0.105,
            fontWeight: 800,
            letterSpacing: -u * 0.002,
            lineHeight: 1.08,
            textAlign: "center",
          }}
        >
          {words.map((word, w) => {
            const before = words
              .slice(0, w)
              .reduce((n, other) => n + other.length + 1, 0);
            return (
              <span key={w} style={{ display: "inline-flex" }}>
                {word.split("").map((ch, i) => {
                  const pop = spring({
                    frame: frame - at(0.01) - (before + i) * stagger,
                    fps,
                    config: { damping: 14, stiffness: 90 },
                    durationInFrames: Math.round(durationInFrames * 0.3),
                  });
                  return (
                    <span
                      key={i}
                      style={{
                        opacity: pop,
                        transform: `translateY(${(1 - pop) * u * 0.09}px)`,
                      }}
                    >
                      {ch}
                    </span>
                  );
                })}
              </span>
            );
          })}
        </div>

        <div
          style={{
            width: u * 0.5 * rule,
            height: Math.max(2, u * 0.004),
            marginTop: u * 0.035,
            borderRadius: u * 0.004,
            background: "linear-gradient(90deg, #818cf8, #f472b6)",
          }}
        />

        <div
          style={{
            marginTop: u * 0.035,
            fontSize: u * 0.036,
            fontWeight: 500,
            color: "#c7d2fe",
            textAlign: "center",
            maxWidth: "86%",
            opacity: tag,
            transform: `translateY(${(1 - tag) * u * 0.03}px)`,
          }}
        >
          {tagline}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_PRODUCT_DEMO = """
import React from "react";
import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

// A step whose image is missing keeps its caption and shows the empty panel.
// Remotion's Img cancels the render when a source fails to load, so a name
// nobody uploaded would otherwise take the whole video down.
function Shot({ name, style }) {
  const [broken, setBroken] = React.useState(false);
  if (!name || broken) return null;
  return (
    <Img
      src={staticFile(name)}
      maxRetries={0}
      onError={() => setBroken(true)}
      style={style}
    />
  );
}

export default function Scene({ title, steps }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const list = (Array.isArray(steps) ? steps : []).filter(Boolean);
  const count = Math.max(1, list.length);
  // The video is divided by however many steps there are, so three steps and
  // seven steps both fill the length that was asked for.
  const seg = durationInFrames / count;
  const fade = Math.max(2, Math.round(seg * 0.18));

  const portrait = height > width * 1.1;
  const frameW = Math.min(width * (portrait ? 0.88 : 0.74), u * 1.02);
  const frameH = frameW * 0.6;
  const bezel = u * 0.014;

  const head = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.06),
  });
  const overall = frame / Math.max(1, durationInFrames - 1);

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#e2e8f0" }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "radial-gradient(120% 90% at 50% 0%, rgba(56,189,248,0.20), rgba(0,0,0,0) 60%)",
          transform: `translateY(${Math.sin(frame / fps * 0.7) * u * 0.02}px)`,
        }}
      />

      <div
        style={{
          position: "absolute",
          top: height * 0.075,
          width: "100%",
          textAlign: "center",
          fontSize: u * 0.04,
          fontWeight: 700,
          letterSpacing: u * 0.0006,
          opacity: head,
          transform: `translateY(${(1 - head) * -u * 0.04}px)`,
        }}
      >
        {title}
      </div>

      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            position: "relative",
            width: frameW,
            height: frameH,
            padding: bezel,
            borderRadius: u * 0.032,
            background: "#111827",
            border: `${Math.max(1, u * 0.002)}px solid rgba(255,255,255,0.14)`,
            boxShadow: `0 ${u * 0.03}px ${u * 0.07}px rgba(0,0,0,0.55)`,
          }}
        >
          <div
            style={{
              position: "absolute",
              inset: bezel,
              borderRadius: u * 0.022,
              overflow: "hidden",
              background: "linear-gradient(140deg, #1e293b, #0f172a)",
            }}
          >
            {list.map((step, i) => {
              const local = frame - i * seg;
              if (local < 0) return null;
              const shown = interpolate(local, [0, fade], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              });
              // The zoom never stops inside a step, which is what keeps a
              // screenshot from reading as a paused video.
              const zoom = interpolate(local, [0, seg], [1.03, 1.16], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              });
              const name = step && step.image ? String(step.image) : "";
              return (
                <div
                  key={i}
                  style={{
                    position: "absolute",
                    inset: 0,
                    opacity: shown,
                    background: "linear-gradient(140deg, #1e293b, #0f172a)",
                  }}
                >
                  <Shot
                    name={name}
                    style={{
                      width: "100%",
                      height: "100%",
                      objectFit: "cover",
                      transform: `scale(${zoom}) translateX(${
                        (zoom - 1) * u * 0.06
                      }px)`,
                    }}
                  />
                </div>
              );
            })}
          </div>
        </div>

        <div
          style={{
            marginTop: u * 0.045,
            width: frameW,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: u * 0.02,
          }}
        >
          {list.map((step, i) => {
            const local = frame - i * seg;
            if (local < 0 || local >= seg) return null;
            const slide = spring({
              frame: local,
              fps,
              config: { damping: 200 },
              durationInFrames: Math.max(2, Math.round(seg * 0.3)),
            });
            return (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: u * 0.022,
                  opacity: slide,
                  transform: `translateY(${(1 - slide) * u * 0.03}px)`,
                }}
              >
                <div
                  style={{
                    flex: "0 0 auto",
                    width: u * 0.062,
                    height: u * 0.062,
                    borderRadius: "50%",
                    background: "#38bdf8",
                    color: "#04141f",
                    fontSize: u * 0.03,
                    fontWeight: 800,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {i + 1}
                </div>
                <div style={{ fontSize: u * 0.034, fontWeight: 600, lineHeight: 1.25 }}>
                  {step && step.caption ? step.caption : ""}
                </div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>

      <div
        style={{
          position: "absolute",
          left: (width - frameW) / 2,
          bottom: height * 0.055,
          width: frameW,
          height: Math.max(2, u * 0.005),
          borderRadius: u * 0.005,
          background: "rgba(255,255,255,0.14)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${overall * 100}%`,
            height: "100%",
            background: "linear-gradient(90deg, #38bdf8, #a78bfa)",
          }}
        />
      </div>
    </AbsoluteFill>
  );
}
"""


_WORKFLOW_EXPLAINER = """
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ title, steps }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const list = (Array.isArray(steps) ? steps : []).filter(Boolean);
  const rows = Math.max(1, list.length);

  // The rows share whatever height is left once the title has its band, so
  // three steps and six steps both fill the frame rather than crowding it.
  const bodyH = height * 0.62;
  const rowH = bodyH / rows;
  const dot = Math.min(rowH * 0.6, u * 0.09);
  const font = Math.min(rowH * 0.3, u * 0.042);
  const railW = Math.max(2, u * 0.004);
  const wash = Math.round(Math.sqrt(width * width + height * height) * 1.05);
  const railTop = rowH / 2;
  const railH = (rows - 1) * rowH;

  const head = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.08),
  });
  const grown = interpolate(frame, [durationInFrames * 0.05, durationInFrames * 0.86], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // A pulse runs the rail on a loop, so the last steps are not watched in
  // front of a frozen diagram.
  const loop = Math.max(1, Math.round(durationInFrames * 0.28));
  const pulse = (frame % loop) / loop;

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#e2e8f0" }}>
      <div
        style={{
          position: "absolute",
          width: wash,
          height: wash,
          left: (width - wash) / 2,
          top: (height - wash) / 2,
          background:
            "radial-gradient(60% 60% at 20% 20%, rgba(45,212,191,0.20), rgba(0,0,0,0) 70%)",
          transform: `rotate(${frame * 0.08}deg)`,
        }}
      />

      <div
        style={{
          position: "absolute",
          top: height * 0.09,
          width: "100%",
          textAlign: "center",
          fontSize: u * 0.048,
          fontWeight: 800,
          opacity: head,
          transform: `translateY(${(1 - head) * -u * 0.04}px)`,
        }}
      >
        {title}
      </div>

      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            position: "relative",
            width: Math.min(width * 0.78, u * 1.2),
            height: bodyH,
            marginTop: height * 0.06,
          }}
        >
          <div
            style={{
              position: "absolute",
              left: dot / 2 - railW / 2,
              top: railTop,
              width: railW,
              height: railH * grown,
              background: "linear-gradient(180deg, #2dd4bf, #6366f1)",
              borderRadius: railW,
            }}
          />
          {railH > 0 ? (
            <div
              style={{
                position: "absolute",
                left: dot / 2 - dot * 0.09,
                top: railTop + railH * grown * pulse - dot * 0.09,
                width: dot * 0.18,
                height: dot * 0.18,
                borderRadius: "50%",
                background: "#f0fdfa",
                boxShadow: `0 0 ${u * 0.02}px rgba(45,212,191,0.9)`,
              }}
            />
          ) : null}

          {list.map((step, i) => {
            const appear = spring({
              frame: frame - durationInFrames * (0.015 + (i * 0.68) / rows),
              fps,
              config: { damping: 200 },
              durationInFrames: Math.round(durationInFrames * 0.12),
            });
            return (
              <div
                key={i}
                style={{
                  position: "absolute",
                  top: i * rowH,
                  left: 0,
                  right: 0,
                  height: rowH,
                  display: "flex",
                  alignItems: "center",
                  opacity: appear,
                  transform: `translateX(${(1 - appear) * u * 0.06}px)`,
                }}
              >
                <div
                  style={{
                    flex: "0 0 auto",
                    width: dot,
                    height: dot,
                    borderRadius: "50%",
                    background: "#0f172a",
                    border: `${Math.max(2, u * 0.004)}px solid #2dd4bf`,
                    color: "#5eead4",
                    fontSize: dot * 0.42,
                    fontWeight: 800,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {i + 1}
                </div>
                <div
                  style={{
                    marginLeft: dot * 0.5,
                    fontSize: font,
                    fontWeight: 600,
                    lineHeight: 1.3,
                  }}
                >
                  {typeof step === "string" ? step : String(step)}
                </div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_FEATURE_LAUNCH = """
import React from "react";
import {
  AbsoluteFill,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ headline, bullets, cta }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const points = (Array.isArray(bullets) ? bullets : []).filter(Boolean);
  const at = (f) => durationInFrames * f;
  const seconds = frame / fps;

  const head = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.12),
  });
  const call = spring({
    frame: frame - at(0.68),
    fps,
    config: { damping: 12, stiffness: 90 },
    durationInFrames: Math.round(durationInFrames * 0.24),
  });
  // The pill keeps breathing once it has landed, so the closing seconds of the
  // video still move.
  const breathe = 1 + Math.sin(seconds * 3.2) * 0.02 * call;
  // A highlight crosses the frame on a loop, from the first frame to the last.
  const sweep = ((frame % Math.max(1, durationInFrames * 0.4)) /
    Math.max(1, durationInFrames * 0.4)) * 1.6 - 0.3;

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#f1f5f9" }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(135deg, rgba(129,140,248,0.22), rgba(0,0,0,0) 55%)",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: -height * 0.3,
          left: width * sweep - u * 0.3,
          width: u * 0.35,
          height: height * 1.6,
          background:
            "linear-gradient(90deg, rgba(255,255,255,0)," +
            "rgba(255,255,255,0.10), rgba(255,255,255,0))",
          transform: "rotate(14deg)",
        }}
      />

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "flex-start",
          padding: `${height * 0.12}px ${width * 0.1}px`,
        }}
      >
        <div
          style={{
            fontSize: u * 0.078,
            fontWeight: 800,
            lineHeight: 1.1,
            letterSpacing: -u * 0.0015,
            opacity: head,
            transform: `translateY(${(1 - head) * u * 0.05}px)`,
          }}
        >
          {headline}
        </div>

        <div style={{ marginTop: u * 0.055, width: "100%" }}>
          {points.map((point, i) => {
            const slide = spring({
              frame: frame - at(0.16) - i * Math.round(durationInFrames * 0.09),
              fps,
              config: { damping: 200 },
              durationInFrames: Math.round(durationInFrames * 0.18),
            });
            return (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  marginBottom: u * 0.03,
                  opacity: slide,
                  transform: `translateX(${(1 - slide) * -u * 0.06}px)`,
                }}
              >
                <div
                  style={{
                    flex: "0 0 auto",
                    width: u * 0.024,
                    height: u * 0.024,
                    borderRadius: "50%",
                    marginRight: u * 0.028,
                    background: "linear-gradient(135deg, #a78bfa, #38bdf8)",
                  }}
                />
                <div style={{ fontSize: u * 0.042, fontWeight: 500, lineHeight: 1.3 }}>
                  {typeof point === "string" ? point : String(point)}
                </div>
              </div>
            );
          })}
        </div>

        <div
          style={{
            marginTop: u * 0.05,
            padding: `${u * 0.026}px ${u * 0.055}px`,
            borderRadius: u * 0.1,
            fontSize: u * 0.042,
            fontWeight: 700,
            color: "#0b1020",
            background: "linear-gradient(135deg, #c4b5fd, #67e8f9)",
            opacity: Math.min(1, call),
            transform: `scale(${(0.85 + call * 0.15) * breathe})`,
            boxShadow: `0 ${u * 0.02}px ${u * 0.05}px rgba(103,232,249,0.25)`,
          }}
        >
          {cta}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_ANNOUNCEMENT = """
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ eyebrow, headline, detail }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const at = (f) => durationInFrames * f;
  const seconds = frame / fps;

  const brow = interpolate(frame, [0, at(0.07)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const head = spring({
    frame: frame - at(0.02),
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.2),
  });
  const note = interpolate(frame, [at(0.36), at(0.54)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // The statement pushes towards the viewer for the whole video. It is the
  // slowest thing on screen and the reason no frame matches the one before.
  const push = interpolate(frame, [0, durationInFrames], [1, 1.07]);

  const blobs = [
    { tint: "rgba(244,63,94,0.45)", size: 1.4, speed: 0.33, phase: 0.4 },
    { tint: "rgba(59,130,246,0.42)", size: 1.15, speed: 0.47, phase: 2.7 },
    { tint: "rgba(250,204,21,0.28)", size: 0.85, speed: 0.71, phase: 5.1 },
  ];

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#ffffff" }}>
      {blobs.map((blob, i) => {
        const angle = seconds * blob.speed + blob.phase;
        const size = u * blob.size;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              width: size,
              height: size,
              left: width / 2 - size / 2 + Math.cos(angle) * width * 0.26,
              top: height / 2 - size / 2 + Math.sin(angle * 1.3) * height * 0.24,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${blob.tint}, rgba(0,0,0,0) 70%)`,
              filter: `blur(${u * 0.06}px)`,
            }}
          />
        );
      })}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background:
            "radial-gradient(75% 75% at 50% 50%, rgba(0,0,0,0) 40%, rgba(0,0,0,0.6))",
        }}
      />

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          padding: u * 0.1,
          textAlign: "center",
          transform: `scale(${push})`,
        }}
      >
        <div
          style={{
            fontSize: u * 0.028,
            fontWeight: 700,
            letterSpacing: u * 0.008,
            textTransform: "uppercase",
            color: "#fecdd3",
            opacity: brow,
          }}
        >
          {eyebrow}
        </div>
        <div
          style={{
            marginTop: u * 0.035,
            fontSize: u * 0.115,
            fontWeight: 800,
            lineHeight: 1.05,
            letterSpacing: -u * 0.002,
            maxWidth: "92%",
            opacity: head,
            transform: `translateY(${(1 - head) * u * 0.06}px)`,
            textShadow: `0 ${u * 0.01}px ${u * 0.04}px rgba(0,0,0,0.45)`,
          }}
        >
          {headline}
        </div>
        <div
          style={{
            marginTop: u * 0.045,
            fontSize: u * 0.034,
            fontWeight: 500,
            color: "#e2e8f0",
            maxWidth: "76%",
            lineHeight: 1.35,
            opacity: note,
            transform: `translateY(${(1 - note) * u * 0.025}px)`,
          }}
        >
          {detail}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_STAT_REVEAL = """
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ title, stats }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const list = (Array.isArray(stats) ? stats : []).filter(Boolean);
  const count = Math.max(1, list.length);
  // Vertical crops put the numbers in a column; anything wider puts them in a
  // row. Read from the config rather than assumed, so both are deliberate.
  const stacked = height > width * 1.1;
  const seconds = frame / fps;

  const head = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.07),
  });
  const wash = Math.round(Math.sqrt(width * width + height * height) * 1.05);
  const gap = u * 0.05;
  const cardW = stacked
    ? width * 0.78
    : Math.min((width * 0.86 - gap * (count - 1)) / count, u * 0.62);
  const inner = cardW - u * 0.06;
  // The longest number any stat will show, so nothing outgrows its card. A
  // percentage with two decimals and a suffix is wider than it looks.
  const widest = list.reduce((most, stat) => {
    const text = String((stat && stat.value) ?? "") + String((stat && stat.suffix) ?? "");
    return Math.max(most, text.length);
  }, 1);
  const numberSize = Math.min(
    stacked ? u * 0.14 : u * 0.15,
    (inner / widest) * 1.5,
    stacked ? (height * 0.6) / count * 0.42 : u * 0.15
  );

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#f8fafc" }}>
      <div
        style={{
          position: "absolute",
          width: wash,
          height: wash,
          left: (width - wash) / 2,
          top: (height - wash) / 2,
          background:
            "conic-gradient(from 0deg, rgba(56,189,248,0.16)," +
            " rgba(168,85,247,0.16), rgba(56,189,248,0.16))",
          transform: `rotate(${frame * 0.12}deg)`,
        }}
      />

      <div
        style={{
          position: "absolute",
          top: height * 0.1,
          width: "100%",
          textAlign: "center",
          fontSize: u * 0.042,
          fontWeight: 700,
          letterSpacing: u * 0.001,
          opacity: head,
          transform: `translateY(${(1 - head) * -u * 0.03}px)`,
        }}
      >
        {title}
      </div>

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          flexDirection: stacked ? "column" : "row",
          gap: u * 0.05,
          padding: `${height * 0.2}px ${width * 0.06}px ${height * 0.1}px`,
        }}
      >
        {list.map((stat, i) => {
          const raw = String((stat && stat.value) ?? "");
          const digits = raw.replace(/[^0-9.]/g, "");
          const target = Number(digits) || 0;
          const decimals = (digits.split(".")[1] || "").length;
          const prefix = raw.slice(0, raw.length - raw.replace(/^[^0-9.]+/, "").length);

          const climb = spring({
            frame: frame - durationInFrames * (0.015 + i * 0.1),
            fps,
            config: { damping: 200 },
            durationInFrames: Math.round(durationInFrames * 0.45),
          });
          // Once a number has landed it floats, on its own phase, so three
          // finished stats are still three moving things.
          const bob = Math.sin(seconds * 1.7 + i * 1.4) * u * 0.008 * climb;
          const shown = (target * climb).toFixed(decimals);

          return (
            <div
              key={i}
              style={{
                width: cardW,
                padding: `${u * 0.04}px ${u * 0.03}px`,
                borderRadius: u * 0.03,
                textAlign: "center",
                background: "rgba(15,23,42,0.66)",
                border: `${Math.max(1, u * 0.0018)}px solid rgba(148,163,184,0.28)`,
                opacity: Math.min(1, climb * 3),
                transform: `translateY(${bob + (1 - climb) * u * 0.04}px)`,
              }}
            >
              <div
                style={{
                  fontSize: numberSize,
                  fontWeight: 800,
                  lineHeight: 1,
                  whiteSpace: "nowrap",
                  letterSpacing: -u * 0.002,
                  background: "linear-gradient(135deg, #7dd3fc, #c084fc)",
                  WebkitBackgroundClip: "text",
                  WebkitTextFillColor: "transparent",
                }}
              >
                {prefix}
                {shown}
                {stat && stat.suffix ? stat.suffix : ""}
              </div>
              <div
                style={{
                  marginTop: u * 0.022,
                  fontSize: numberSize * 0.24,
                  fontWeight: 500,
                  color: "#cbd5e1",
                  lineHeight: 1.3,
                  // Two lines' worth whether the label needs them or not, so
                  // the underlines of three cards sit on one line.
                  minHeight: numberSize * 0.24 * 2.6,
                }}
              >
                {stat && stat.label ? stat.label : ""}
              </div>
              <div
                style={{
                  margin: `${u * 0.022}px auto 0`,
                  width: `${interpolate(climb, [0, 1], [0, 60])}%`,
                  height: Math.max(2, u * 0.004),
                  borderRadius: u * 0.004,
                  background: "linear-gradient(90deg, #38bdf8, #a855f7)",
                }}
              />
            </div>
          );
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_QUOTE = """
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ quote, name, role }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const at = (f) => durationInFrames * f;
  const seconds = frame / fps;

  const words = String(quote || "").split(" ").filter(Boolean);
  // The words arrive across most of the video rather than in the first
  // second, which is what makes a testimonial read at the pace it is spoken.
  const span = Math.max(1, at(0.66) - at(0.02));
  const per = span / Math.max(1, words.length);

  const mark = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.08),
  });
  const credit = interpolate(frame, [at(0.72), at(0.86)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const drift = interpolate(frame, [0, durationInFrames], [u * 0.015, -u * 0.015]);

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#f8fafc" }}>
      <div
        style={{
          position: "absolute",
          width: u * 1.3,
          height: u * 1.3,
          left: width / 2 - u * 0.65 + Math.cos(seconds * 0.4) * u * 0.2,
          top: height / 2 - u * 0.65 + Math.sin(seconds * 0.52) * u * 0.16,
          borderRadius: "50%",
          background:
            "radial-gradient(circle, rgba(251,191,36,0.24), rgba(0,0,0,0) 68%)",
          filter: `blur(${u * 0.05}px)`,
        }}
      />

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          padding: `${height * 0.12}px ${width * 0.11}px`,
          transform: `translateY(${drift}px)`,
        }}
      >
        <div
          style={{
            fontSize: u * 0.18,
            lineHeight: 0.7,
            fontWeight: 800,
            color: "rgba(251,191,36,0.85)",
            opacity: mark,
            transform: `scale(${0.7 + mark * 0.3}) rotate(${
              Math.sin(seconds * 0.9) * 3
            }deg)`,
          }}
        >
          {"\\u201c"}
        </div>

        <div
          style={{
            marginTop: u * 0.03,
            fontSize: u * 0.055,
            fontWeight: 600,
            lineHeight: 1.32,
            textAlign: "center",
            maxWidth: "90%",
          }}
        >
          {words.map((word, i) => {
            const on = interpolate(
              frame,
              [at(0.02) + i * per, at(0.02) + i * per + per * 2.2],
              [0, 1],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
            );
            return (
              <span
                key={i}
                style={{
                  display: "inline-block",
                  marginRight: u * 0.014,
                  opacity: on,
                  transform: `translateY(${(1 - on) * u * 0.02}px)`,
                }}
              >
                {word}
              </span>
            );
          })}
        </div>

        <div
          style={{
            marginTop: u * 0.06,
            width: u * 0.12 * credit,
            height: Math.max(2, u * 0.004),
            borderRadius: u * 0.004,
            background: "#fbbf24",
          }}
        />
        <div
          style={{
            marginTop: u * 0.03,
            fontSize: u * 0.036,
            fontWeight: 700,
            opacity: credit,
            transform: `translateY(${(1 - credit) * u * 0.02}px)`,
          }}
        >
          {name}
        </div>
        <div
          style={{
            marginTop: u * 0.012,
            fontSize: u * 0.028,
            fontWeight: 500,
            color: "#cbd5e1",
            opacity: credit,
          }}
        >
          {role}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


_TITLE_CARD = """
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';

export default function Scene({ line1, line2, accent }) {
  const frame = useCurrentFrame();
  const { width, height, fps, durationInFrames } = useVideoConfig();

  const u = Math.min(width, height);
  const at = (f) => durationInFrames * f;
  const seconds = frame / fps;
  const tint = accent || "#f97316";

  const first = spring({
    frame,
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.18),
  });
  const second = spring({
    frame: frame - at(0.12),
    fps,
    config: { damping: 200 },
    durationInFrames: Math.round(durationInFrames * 0.22),
  });
  const bar = interpolate(frame, [at(0.42), at(0.72)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // The grid never stops travelling, which is what a section break needs: the
  // type settles early and the card still has to be moving when it cuts.
  const grid = (frame * u * 0.0018) % (u * 0.12);
  // Words first, letters inside them: a wrapping row of single letters
  // splits a word across two lines at the frame edge.
  const words = String(line2 || "").split(" ");

  return (
    <AbsoluteFill style={{ fontFamily: FONT, color: "#f8fafc", overflow: "hidden" }}>
      <div
        style={{
          position: "absolute",
          inset: `-${u * 0.2}px`,
          backgroundImage:
            "linear-gradient(rgba(148,163,184,0.16) 1px, rgba(0,0,0,0) 1px)," +
            "linear-gradient(90deg, rgba(148,163,184,0.16) 1px, rgba(0,0,0,0) 1px)",
          backgroundSize: `${u * 0.12}px ${u * 0.12}px`,
          transform: `translate(${grid}px, ${grid}px)`,
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(70% 70% at 50% 50%, ${tint}22, rgba(0,0,0,0) 70%)`,
          opacity: 0.6 + Math.sin(seconds * 1.3) * 0.2,
        }}
      />

      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          padding: u * 0.1,
          textAlign: "center",
        }}
      >
        <div style={{ overflow: "hidden", paddingBottom: u * 0.012 }}>
          <div
            style={{
              fontSize: u * 0.045,
              fontWeight: 600,
              letterSpacing: u * 0.006,
              textTransform: "uppercase",
              color: "#cbd5e1",
              transform: `translateY(${(1 - first) * 100}%)`,
            }}
          >
            {line1}
          </div>
        </div>

        <div
          style={{
            marginTop: u * 0.025,
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            gap: `0 ${u * 0.03}px`,
            fontSize: u * 0.11,
            fontWeight: 800,
            lineHeight: 1.06,
            letterSpacing: -u * 0.002,
            color: tint,
          }}
        >
          {words.map((word, w) => {
            const before = words
              .slice(0, w)
              .reduce((n, other) => n + other.length + 1, 0);
            return (
              <span key={w} style={{ display: "inline-flex" }}>
                {word.split("").map((ch, i) => (
                  <span
                    key={i}
                    style={{
                      opacity: second,
                      transform: `translateY(${
                        (1 - second) * u * 0.08 +
                        Math.sin(seconds * 2.2 + (before + i) * 0.35) *
                          u * 0.006 * second
                      }px)`,
                    }}
                  >
                    {ch}
                  </span>
                ))}
              </span>
            );
          })}
        </div>

        <div
          style={{
            marginTop: u * 0.04,
            width: u * 0.55,
            height: Math.max(3, u * 0.006),
            borderRadius: u * 0.006,
            background: "rgba(255,255,255,0.14)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${bar * 100}%`,
              height: "100%",
              background: tint,
            }}
          />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
"""


TEMPLATES: dict[str, Template] = {
    "brand_intro": Template(
        label="Brand intro",
        description="Opens a website hero or a channel with your name and one line about it.",
        scene=_BRAND_INTRO,
        props={
            "brand": "Northwind Studio",
            "tagline": "Interfaces that ship on Friday",
            "logo": "",
        },
        duration_seconds=5.0,
        background="#0b1020",
    ),
    "product_demo": Template(
        label="Product demo",
        description="Walks through screenshots of your product, one caption per step.",
        scene=_PRODUCT_DEMO,
        props={
            "title": "Ship your first workflow in three minutes",
            "steps": [
                {"caption": "Create a workspace and invite the team", "image": "screen-1.png"},
                {"caption": "Drop in the nodes you need", "image": "screen-2.png"},
                {"caption": "Run it and watch every step", "image": "screen-3.png"},
            ],
        },
        duration_seconds=12.0,
        background="#080d1a",
    ),
    "workflow_explainer": Template(
        label="How it works",
        description="Numbered steps down a connecting line, for explaining a process.",
        scene=_WORKFLOW_EXPLAINER,
        props={
            "title": "How Northwind moves a ticket",
            "steps": [
                "A customer writes in, and the ticket lands in the queue",
                "The agent reads the history and drafts a reply",
                "A human approves it, or edits it first",
                "The reply goes out and the ticket closes itself",
            ],
        },
        duration_seconds=12.0,
        background="#071a1a",
    ),
    "feature_launch": Template(
        label="Feature launch",
        description="A headline, three reasons to care, and the thing you want people to do.",
        scene=_FEATURE_LAUNCH,
        props={
            "headline": "Scheduled runs are here",
            "bullets": [
                "Put any workflow on a timetable",
                "See every run and what it cost",
                "Retries when a provider goes quiet",
            ],
            "cta": "Try it free for 14 days",
        },
        duration_seconds=10.0,
        background="#0d0b22",
    ),
    "announcement": Template(
        label="Announcement",
        description="One sentence you want remembered, on a background that keeps moving.",
        scene=_ANNOUNCEMENT,
        props={
            "eyebrow": "Product update",
            "headline": "Version 3 is live",
            "detail": "Faster runs, clearer costs, and an editor that finally undoes properly.",
        },
        duration_seconds=6.0,
        background="#120a1e",
    ),
    "stat_reveal": Template(
        label="Numbers",
        description="Two or three figures counting up, for results a slide would bury.",
        scene=_STAT_REVEAL,
        props={
            "title": "What the first year looked like",
            "stats": [
                {"value": "1.4", "label": "Runs completed", "suffix": "M"},
                {"value": "99.95", "label": "Uptime across all regions", "suffix": "%"},
                {"value": "38", "label": "Hours saved per team each month", "suffix": "h"},
            ],
        },
        duration_seconds=8.0,
        background="#050b16",
    ),
    "quote": Template(
        label="Customer quote",
        description="A testimonial that arrives at reading pace, with who said it.",
        scene=_QUOTE,
        props={
            "quote": (
                "We replaced four scripts and a cron job with one workflow, "
                "and nobody has been paged since."
            ),
            "name": "Priya Raman",
            "role": "Head of Platform, Ferrolane",
        },
        duration_seconds=9.0,
        background="#16110a",
    ),
    "title_card": Template(
        label="Title card",
        description="Kinetic type for the opening of a video or the break between sections.",
        scene=_TITLE_CARD,
        props={
            "line1": "Chapter two",
            "line2": "Building the pipeline",
            "accent": "#f97316",
        },
        duration_seconds=4.0,
        background="#0a0f1c",
    ),
}

#: The order a picker shows them in: the two most people want first, the
#: section furniture last.
TEMPLATE_CHOICES: tuple[str, ...] = (
    "brand_intro",
    "product_demo",
    "workflow_explainer",
    "feature_launch",
    "announcement",
    "stat_reveal",
    "quote",
    "title_card",
)
