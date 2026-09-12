import React from "react";
import {
  AbsoluteFill,
  Easing,
  Img,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const FONT =
  'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
const INK = "#101322";
const MUTED = "#5b627a";
const BRAND = "#6d4aff";
const GOOD = "#0f9d68";

/** Everything eases the same way, so the whole film feels like one hand. */
const EASE = Easing.bezier(0.22, 0.61, 0.36, 1);

/**
 * A camera over a screenshot.
 *
 * The screenshot is 2560 wide and the frame is 1920, so there is real detail
 * to move into. `from` and `to` are rectangles in the SOURCE image, given as
 * fractions, and the camera travels between them. Showing a whole screen
 * shrunk to fit is what made the first cut of this video unreadable.
 */
/**
 * A camera over a screenshot, framing a measured rectangle.
 *
 * `from` and `to` are rectangles in the SOURCE image, in fractions, taken
 * from the real page with getBoundingClientRect rather than guessed. The
 * camera works out the zoom that puts that rectangle in the middle of the
 * frame with a margin, and never goes below the zoom that covers the frame,
 * because a screenshot that does not cover leaves black bars down the sides.
 */
const Camera: React.FC<{
  file: string;
  aspect: number;
  from: Rect;
  to: Rect;
  length: number;
  pad?: number;
  children?: React.ReactNode;
}> = ({ file, aspect, from, to, length, pad = 0.08, children }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const progress = interpolate(frame, [0, length], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: EASE,
  });

  const lerp = (a: number, b: number) => a + (b - a) * progress;
  const rect = {
    x: lerp(from.x, to.x),
    y: lerp(from.y, to.y),
    w: lerp(from.w, to.w),
    h: lerp(from.h, to.h),
  };

  // How wide the image has to be drawn for the rectangle to fill the frame
  // minus the margin, in each direction, and then the wider of the two so
  // nothing important is cut off.
  const byWidth = ((1 - pad * 2) * width) / Math.max(rect.w, 0.001);
  const byHeight = (aspect * (1 - pad * 2) * height) / Math.max(rect.h, 0.001);
  const cover = Math.max(width, height * aspect);
  const drawn = Math.max(cover, Math.min(byWidth, byHeight));
  const drawnHeight = drawn / aspect;

  const centreX = rect.x + rect.w / 2;
  const centreY = rect.y + rect.h / 2;
  // Kept inside the image, so a rectangle near an edge pans as far as it can
  // rather than showing the background beyond it.
  const left = clamp(width / 2 - centreX * drawn, width - drawn, 0);
  const top = clamp(height / 2 - centreY * drawnHeight, height - drawnHeight, 0);

  return (
    <AbsoluteFill style={{ overflow: "hidden", backgroundColor: "#eef1f7" }}>
      <div style={{ position: "absolute", width: drawn, height: drawnHeight, left, top }}>
        <Img src={staticFile(file)} style={{ width: "100%", display: "block" }} />
        {children}
      </div>
    </AbsoluteFill>
  );
};

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const clamp = (value: number, low: number, high: number) =>
  Math.max(Math.min(low, high), Math.min(Math.max(low, high), value));

/**
 * A ring around the thing being talked about.
 *
 * Drawn in the SOURCE image's coordinates, inside the camera, so it stays on
 * its element while the camera pushes in. Placed by hand it drifted off the
 * moment anything zoomed, which is how the first cut ended up with a purple
 * box around empty canvas.
 */
const Ring: React.FC<{ rect: Rect; from?: number; label?: string }> = ({
  rect,
  from = 0,
  label,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const draw = spring({ frame: frame - from, fps, config: { damping: 20 } });
  const unit = height / 46;
  if (draw < 0.01) return null;
  return (
    <div
      style={{
        position: "absolute",
        left: `${rect.x * 100}%`,
        top: `${rect.y * 100}%`,
        width: `${rect.w * 100}%`,
        height: `${rect.h * 100}%`,
        border: `${Math.max(2, unit * 0.12)}px solid ${BRAND}`,
        borderRadius: unit * 0.55,
        boxShadow: `0 0 0 ${unit * 0.5}px ${BRAND}1f`,
        opacity: draw,
      }}
    >
      {label ? (
        <div
          style={{
            position: "absolute",
            left: 0,
            top: `-${unit * 2.6}px`,
            background: BRAND,
            color: "white",
            fontFamily: FONT,
            fontWeight: 650,
            fontSize: unit * 1.05,
            padding: `${unit * 0.28}px ${unit * 0.72}px`,
            borderRadius: unit * 0.4,
            whiteSpace: "nowrap",
          }}
        >
          {label}
        </div>
      ) : null}
    </div>
  );
};

/** The line of the beat, bottom left, over a soft plate so it stays legible. */
const Lower: React.FC<{ step: string; line: string; detail?: string; from?: number }> = ({
  step,
  line,
  detail,
  from = 0,
}) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const unit = height / 46;
  const rise = spring({ frame: frame - from, fps, config: { damping: 22, mass: 0.8 } });
  return (
    <div
      style={{
        position: "absolute",
        left: unit * 3,
        bottom: unit * 3,
        opacity: rise,
        transform: `translateY(${(1 - rise) * unit * 1.4}px)`,
        background: "rgba(255,255,255,0.92)",
        backdropFilter: "blur(8px)",
        borderRadius: unit * 0.9,
        padding: `${unit * 1.1}px ${unit * 1.6}px`,
        boxShadow: "0 18px 50px rgba(16,19,34,0.16)",
        maxWidth: "62%",
      }}
    >
      <p
        style={{
          margin: 0,
          fontFamily: FONT,
          fontSize: unit * 0.92,
          letterSpacing: unit * 0.14,
          textTransform: "uppercase",
          color: BRAND,
          fontWeight: 700,
        }}
      >
        {step}
      </p>
      <p
        style={{
          margin: `${unit * 0.45}px 0 0`,
          fontFamily: FONT,
          fontSize: unit * 2.2,
          fontWeight: 680,
          color: INK,
          lineHeight: 1.15,
        }}
      >
        {line}
      </p>
      {detail ? (
        <p
          style={{
            margin: `${unit * 0.35}px 0 0`,
            fontFamily: FONT,
            fontSize: unit * 1.15,
            color: MUTED,
          }}
        >
          {detail}
        </p>
      ) : null}
    </div>
  );
};

/** Fades a beat in and out so no cut lands on an empty frame. */
const Beat: React.FC<{ children: React.ReactNode; length: number; edge?: number }> = ({
  children,
  length,
  edge = 8,
}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(
    frame,
    [0, edge, Math.max(edge + 1, length - edge), length],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
};

export default function Scene({ tagline, url, consoleUrl, pr, shots, voiceOver }) {
  const { durationInFrames, height } = useVideoConfig();
  const unit = height / 46;
  const beat = durationInFrames / 100;
  const at = (percent: number) => Math.round(beat * percent);
  const OVERLAP = 10;
  const len = (from: number, to: number) => Math.max(1, at(to) - at(from) + OVERLAP);
  const showLower = !voiceOver;

  return (
    <AbsoluteFill style={{ backgroundColor: "#eef1f7" }}>
      {/* Who this is. Three seconds, no longer: the viewer came for the tool. */}
      <Sequence from={at(0)} durationInFrames={len(0, 4)}>
        <Beat length={len(0, 4)}>
          <Opening unit={unit} tagline={tagline} />
        </Beat>
      </Sequence>

      {/* Every way a flow can start, together, because leading with one of
          them made the whole product look like a GitHub bot. */}
      <Sequence from={at(4)} durationInFrames={len(4, 18)}>
        <Beat length={len(4, 18)}>
          <TriggerWall unit={unit} />
          {showLower ? (
            <Lower
              step="It starts"
              line="However your work starts"
              detail="A ticket, a webhook, a timetable, a message. Nothing to wire up by hand."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* The pipeline drawing itself, one node at a time. */}
      <Sequence from={at(18)} durationInFrames={len(18, 26)}>
        <Beat length={len(18, 26)}>
          <Pipeline unit={unit} />
          {showLower ? (
            <Lower
              step="It runs"
              line="Draw it once, on one canvas"
              detail="Eighteen node types. Agents, Python, HTTP, renders, posts, apps."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* What the agent node actually holds. */}
      <Sequence from={at(26)} durationInFrames={len(26, 37)}>
        <Beat length={len(26, 37)}>
          <AgentCard unit={unit} />
          {showLower ? (
            <Lower
              step="It thinks"
              line="An agent, not a prompt box"
              detail="Tools, skills, sub-agents, MCP servers, hand over. Your keys, or ours for free."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* Three real files this product rendered. */}
      <Sequence from={at(37)} durationInFrames={len(37, 44)}>
        <Beat length={len(37, 44)}>
          <Outputs unit={unit} />
          {showLower ? (
            <Lower
              step="It makes"
              line="Video, not only text"
              detail="React in, an MP4 out. Renders, montages, a voice over the top."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* The App Builder: the newest half of the product, and the only part a
          person uses without drawing anything. */}
      <Sequence from={at(44)} durationInFrames={len(44, 67)}>
        <Beat length={len(44, 67)}>
          <AbsoluteFill style={{ backgroundColor: "#eef1f7" }} />
          <AppBuilder unit={unit} />
          {showLower ? (
            <Lower
              step="It builds"
              line="Describe an app, watch it appear"
              detail="A real build at a real address. Deploy it, unpublish it, or take the code."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* And then it delivers, which is the part demos usually skip. */}
      <Sequence from={at(67)} durationInFrames={len(67, 74)}>
        <Beat length={len(67, 74)}>
          <Delivery unit={unit} pr={pr} />
          {showLower ? (
            <Lower
              step="It delivers"
              line="Somewhere a person will see it"
              detail="A pull request, a comment, or a post to Telegram, Discord, Slack, Mastodon, Bluesky."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* The real run page, with the real numbers on it. */}
      <Sequence from={at(74)} durationInFrames={len(74, 78)}>
        <Beat length={len(74, 78)}>
          <Camera
            file={shots.run.file}
            aspect={shots.run.aspect}
            from={shots.run.page}
            to={shots.run.timeline}
            length={len(74, 78)}
          >
            <Ring rect={grow(shots.run.autofix, 1.03)} from={26} label="36.0s, tokens and cost" />
          </Camera>
          {showLower ? (
            <Lower
              step="You watch"
              line="Every node reports itself"
              detail="Status, duration, tokens and what they cost, while the run is going."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      {/* The two numbers that are the argument. */}
      <Sequence from={at(78)} durationInFrames={len(78, 83)}>
        <Beat length={len(78, 83)}>
          <AbsoluteFill style={{ backgroundColor: "#eef1f7" }} />
          <Receipt unit={unit} />
        </Beat>
      </Sequence>

      {/* What this replaces, and what it saves. */}
      <Sequence from={at(83)} durationInFrames={len(83, 96)}>
        <Beat length={len(83, 96)}>
          <AbsoluteFill style={{ backgroundColor: "#eef1f7" }} />
          <Compare unit={unit} />
          {showLower ? (
            <Lower
              step="Compared"
              line="Less to set up, less to run"
              detail="The wiring other builders leave to you is the part we do for you."
              from={8}
            />
          ) : null}
        </Beat>
      </Sequence>

      <Sequence from={at(96)}>
        <Beat length={durationInFrames - at(96)}>
          <EndCard unit={unit} url={url} consoleUrl={consoleUrl} />
        </Beat>
      </Sequence>
    </AbsoluteFill>
  );
}


/** A stroked glyph, drawn on the same 24 grid the console's node icons use. */
const Glyph: React.FC<{ path: string; size: number; color: string }> = ({ path, size, color }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color}
       strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
    <path d={path} />
  </svg>
);

const ICONS = {
  github: "M9 19c-4 1.4-4-2.2-5.5-2.8M15 21v-3.6a3 3 0 0 0-.9-2.4c3-.3 6-1.5 6-6.4a5 5 0 0 0-1.3-3.5 4.7 4.7 0 0 0-.1-3.5s-1.1-.3-3.7 1.4a12.6 12.6 0 0 0-6.6 0C6.2 1.3 5.1 1.6 5.1 1.6a4.7 4.7 0 0 0-.1 3.5A5 5 0 0 0 3.7 8.6c0 4.9 3 6 6 6.4a3 3 0 0 0-.9 2.3V21",
  jira: "M12 2.5 21 11l-4.5 4.5L12 11l-4.5 4.5L3 11zM12 12.5l4.5 4.5L12 21.5 7.5 17z",
  webhook: "M9 9a3 3 0 1 1 4.6 2.5l2.6 4.4M6.5 12.6 4.2 16.6a3 3 0 1 0 3.9 4M12 17h5.5a3 3 0 1 0-2.3-4.9",
  schedule: "M12 3.5a8.5 8.5 0 1 1 0 17 8.5 8.5 0 0 1 0-17zM12 7.5V12l3 2",
  telegram: "M21 4.5 3.5 11.2l4.8 1.7 1.8 5.6 2.6-3.2 4.4 3.2z M8.3 12.9 18 6.8l-7.4 7.7",
  manual: "M8 4.5v7M8 11.5V19a2.5 2.5 0 0 0 2.5 2.5h4A5.5 5.5 0 0 0 20 16v-4M11.5 11V6M15 11.5V7.5M18.5 12V9",
  agent: "M8.5 8.5h7v7h-7zM12 3.5V8M12 16v4.5M3.5 12H8M16 12h4.5M8.5 5v3.5M15.5 5v3.5M8.5 15.5V19M15.5 15.5V19",
  code: "M9.5 6 5 12l4.5 6M14.5 6 19 12l-4.5 6",
  video: "M3.5 6.5h11v11h-11zM14.5 10l6-3v10l-6-3z",
  post: "M21 3.5 3.5 10.2l7 2.8 2.8 7z M21 3.5 10.5 13",
  speech: "M12 3.5a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0v-5a3 3 0 0 1 3-3zM5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3",
} as const;

/** One node card, drawn the way the real canvas draws them. */
const NodeCard: React.FC<{
  unit: number; icon: string; name: string; note: string;
  show: number; done?: number; width?: number;
}> = ({ unit, icon, name, note, show, done = 0, width = 15 }) => (
  <div
    style={{
      width: unit * width,
      background: "white",
      borderRadius: unit * 0.9,
      border: `1px solid rgba(16,19,34,${0.06 + done * 0.02})`,
      boxShadow: `0 ${unit * 0.8}px ${unit * 2.4}px rgba(16,19,34,0.12)`,
      padding: `${unit * 1.05}px ${unit * 1.2}px`,
      opacity: show,
      transform: `translateY(${(1 - show) * unit * 1.6}px) scale(${0.94 + show * 0.06})`,
    }}
  >
    <div style={{ display: "flex", alignItems: "center", gap: unit * 0.7 }}>
      <div
        style={{
          width: unit * 2.2,
          height: unit * 2.2,
          borderRadius: unit * 0.6,
          background: `${BRAND}14`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Glyph path={icon} size={unit * 1.3} color={BRAND} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ margin: 0, fontFamily: FONT, fontSize: unit * 1.05, fontWeight: 680, color: INK }}>
          {name}
        </p>
        <p style={{ margin: `${unit * 0.15}px 0 0`, fontFamily: MONO, fontSize: unit * 0.8, color: MUTED }}>
          {note}
        </p>
      </div>
      <div
        style={{
          width: unit * 0.7,
          height: unit * 0.7,
          borderRadius: "50%",
          background: done > 0.5 ? GOOD : "rgba(16,19,34,0.12)",
          transform: `scale(${0.7 + done * 0.3})`,
        }}
      />
    </div>
  </div>
);

/** Every way a flow can start, on screen at once. */
const TriggerWall: React.FC<{ unit: number }> = ({ unit: base }) => {
  const unit = base * 1.3;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cards = [
    { icon: ICONS.github, name: "GitHub issue", note: "issues.opened" },
    { icon: ICONS.jira, name: "Jira ticket", note: "issue_created" },
    { icon: ICONS.webhook, name: "Your own webhook", note: "POST /hooks/..." },
    { icon: ICONS.schedule, name: "A timetable", note: "0 9 * * mon" },
    { icon: ICONS.telegram, name: "Telegram message", note: "bot update" },
    { icon: ICONS.manual, name: "You, pressing run", note: "trigger.manual" },
  ];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, auto)",
          gap: unit * 1.5,
          marginBottom: unit * 3.6,
        }}
      >
        {cards.map((card, index) => {
          const show = spring({ frame: frame - index * 4, fps, config: { damping: 20 } });
          const lit = interpolate(
            frame,
            [26 + index * 3, 34 + index * 3],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
          );
          return (
            <NodeCard
              key={card.name}
              unit={unit}
              icon={card.icon}
              name={card.name}
              note={card.note}
              show={show}
              done={lit}
              width={16}
            />
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** The pipeline, drawing itself left to right, then running. */
const Pipeline: React.FC<{ unit: number }> = ({ unit: base }) => {
  const unit = base * 1.12;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const nodes = [
    { icon: ICONS.github, name: "GitHub issue", note: "trigger.webhook" },
    { icon: ICONS.agent, name: "AI Agent", note: "agent.llm" },
    { icon: ICONS.video, name: "AI Video", note: "video.ai" },
    { icon: ICONS.post, name: "Post to Social", note: "social.post" },
  ];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: unit * 3.6 }}>
        {nodes.map((node, index) => {
          const show = spring({ frame: frame - index * 9, fps, config: { damping: 20 } });
          const done = interpolate(
            frame,
            [46 + index * 7, 54 + index * 7],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
          );
          const drawn = interpolate(
            frame,
            [index * 9 + 4, index * 9 + 12],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
          );
          return (
            <React.Fragment key={node.name}>
              {index > 0 ? (
                <div
                  style={{
                    width: unit * 2.6,
                    height: 2,
                    background: `linear-gradient(90deg, ${BRAND}, ${BRAND})`,
                    transform: `scaleX(${drawn})`,
                    transformOrigin: "left center",
                    opacity: 0.45,
                  }}
                />
              ) : null}
              <NodeCard
                unit={unit}
                icon={node.icon}
                name={node.name}
                note={node.note}
                show={show}
                done={done}
                width={13.5}
              />
            </React.Fragment>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** What sits inside the agent node, spelled out. */
const AgentCard: React.FC<{ unit: number }> = ({ unit: base }) => {
  const unit = base * 1.3;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 20 } });
  const chips = ["Tools", "Skills", "Sub-agents", "MCP servers", "Hand over", "Chat memory"];
  const models = ["OpenAI", "Anthropic", "Gemini", "Groq", "Ollama"];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          width: unit * 44,
          background: "white",
          borderRadius: unit * 1.2,
          boxShadow: `0 ${unit}px ${unit * 3}px rgba(16,19,34,0.14)`,
          padding: unit * 2.2,
          marginBottom: unit * 3.2,
          opacity: rise,
          transform: `translateY(${(1 - rise) * unit * 2}px)`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: unit * 0.9 }}>
          <div
            style={{
              width: unit * 3,
              height: unit * 3,
              borderRadius: unit * 0.8,
              background: `${BRAND}14`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Glyph path={ICONS.agent} size={unit * 1.8} color={BRAND} />
          </div>
          <div>
            <p style={{ margin: 0, fontFamily: FONT, fontSize: unit * 1.7, fontWeight: 700, color: INK }}>
              AI Agent
            </p>
            <p style={{ margin: `${unit * 0.2}px 0 0`, fontFamily: MONO, fontSize: unit * 0.95, color: MUTED }}>
              agent.llm
            </p>
          </div>
        </div>

        <div style={{ display: "flex", flexWrap: "wrap", gap: unit * 0.6, marginTop: unit * 1.6 }}>
          {chips.map((chip, index) => {
            const show = spring({ frame: frame - 8 - index * 4, fps, config: { damping: 20 } });
            return (
              <span
                key={chip}
                style={{
                  fontFamily: FONT,
                  fontSize: unit * 1.05,
                  fontWeight: 620,
                  color: BRAND,
                  background: `${BRAND}0f`,
                  borderRadius: unit * 2,
                  padding: `${unit * 0.4}px ${unit * 1}px`,
                  opacity: show,
                  transform: `translateY(${(1 - show) * unit * 0.5}px)`,
                }}
              >
                {chip}
              </span>
            );
          })}
        </div>

        <div
          style={{
            marginTop: unit * 1.8,
            paddingTop: unit * 1.2,
            borderTop: "1px solid rgba(16,19,34,0.08)",
            display: "flex",
            alignItems: "baseline",
            gap: unit * 0.8,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontFamily: FONT, fontSize: unit * 1.05, color: MUTED }}>
            Your keys, any provider:
          </span>
          {models.map((model, index) => {
            const show = spring({ frame: frame - 26 - index * 3, fps, config: { damping: 20 } });
            return (
              <span
                key={model}
                style={{
                  fontFamily: FONT,
                  fontSize: unit * 1.1,
                  fontWeight: 650,
                  color: INK,
                  opacity: show,
                }}
              >
                {model}
              </span>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

/** Where the result lands: a real pull request, and the places it can post. */
const Delivery: React.FC<{ unit: number; pr: Record<string, unknown> }> = ({ unit: base, pr }) => {
  const unit = base * 1.25;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = spring({ frame, fps, config: { damping: 18 } });
  const tick = spring({ frame: frame - 10, fps, config: { damping: 12 } });
  const platforms = ["Telegram", "Discord", "Slack", "Mastodon", "Bluesky"];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ display: "flex", alignItems: "center", gap: unit * 3, marginBottom: unit * 3.2 }}>
        <div
          style={{
            width: unit * 34,
            background: "white",
            borderRadius: unit * 1.1,
            padding: unit * 1.8,
            boxShadow: `0 ${unit}px ${unit * 3}px rgba(16,19,34,0.13)`,
            opacity: rise,
            transform: `translateY(${(1 - rise) * unit * 1.6}px)`,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: unit * 0.7 }}>
            <div
              style={{
                width: unit * 1.7,
                height: unit * 1.7,
                borderRadius: "50%",
                background: GOOD,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                transform: `scale(${0.4 + tick * 0.6})`,
              }}
            >
              <svg width={unit} height={unit} viewBox="0 0 24 24" fill="none" stroke="white"
                   strokeWidth={4} strokeLinecap="round" strokeLinejoin="round">
                <path d="M4.5 12.5l5 5 10-10" />
              </svg>
            </div>
            <span style={{ fontFamily: FONT, fontSize: unit * 1.05, color: GOOD, fontWeight: 700 }}>
              Pull request #{String(pr.number)} opened
            </span>
          </div>
          <p style={{ fontFamily: FONT, fontSize: unit * 1.5, fontWeight: 680, color: INK, margin: `${unit}px 0 0` }}>
            {String(pr.title)}
          </p>
          <p style={{ fontFamily: MONO, fontSize: unit * 0.95, color: MUTED, margin: `${unit * 0.6}px 0 0` }}>
            {String(pr.branch)}
          </p>
          <p style={{ fontFamily: MONO, fontSize: unit * 0.95, color: MUTED, margin: `${unit * 0.4}px 0 0` }}>
            <span style={{ color: GOOD }}>+{String(pr.added)}</span>{" "}
            <span style={{ color: "#c0392b" }}>-{String(pr.removed)}</span> across {String(pr.files)} files
          </p>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: unit * 0.7 }}>
          {platforms.map((name, index) => {
            const show = spring({ frame: frame - 12 - index * 4, fps, config: { damping: 20 } });
            return (
              <div
                key={name}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: unit * 0.7,
                  background: "white",
                  borderRadius: unit * 2,
                  padding: `${unit * 0.55}px ${unit * 1.3}px`,
                  boxShadow: `0 ${unit * 0.5}px ${unit * 1.6}px rgba(16,19,34,0.10)`,
                  opacity: show,
                  transform: `translateX(${(1 - show) * unit * 1.6}px)`,
                }}
              >
                <Glyph path={ICONS.post} size={unit * 1.1} color={BRAND} />
                <span style={{ fontFamily: FONT, fontSize: unit * 1.15, fontWeight: 650, color: INK }}>
                  {name}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};

/** The last card: the mark, and the two addresses. */
const EndCard: React.FC<{ unit: number; url: string; consoleUrl: string }> = ({
  unit,
  url,
  consoleUrl,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const mark = spring({ frame, fps, config: { damping: 15, mass: 0.8 } });
  const name = spring({ frame: frame - 7, fps, config: { damping: 20 } });
  const links = spring({ frame: frame - 16, fps, config: { damping: 22 } });
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <Img
        src={staticFile("mark.png")}
        style={{
          width: unit * 6,
          opacity: mark,
          transform: `translateY(${(1 - mark) * unit}px)`,
        }}
      />
      <p
        style={{
          fontFamily: FONT,
          fontSize: unit * 4.4,
          fontWeight: 720,
          color: INK,
          letterSpacing: -unit * 0.08,
          margin: `${unit * 1.2}px 0 0`,
          opacity: name,
        }}
      >
        Basivo
      </p>
      <p
        style={{
          fontFamily: FONT,
          fontSize: unit * 1.5,
          color: MUTED,
          margin: `${unit * 0.6}px 0 0`,
          opacity: name,
        }}
      >
        Open source. One server. Your keys.
      </p>
      <div style={{ display: "flex", gap: unit * 1.2, marginTop: unit * 2, opacity: links }}>
        {[url, consoleUrl].map((link) => (
          <span
            key={link}
            style={{
              fontFamily: MONO,
              fontSize: unit * 1.5,
              fontWeight: 650,
              color: BRAND,
              background: "white",
              borderRadius: unit * 2,
              padding: `${unit * 0.6}px ${unit * 1.6}px`,
              boxShadow: `0 ${unit * 0.6}px ${unit * 2}px rgba(16,19,34,0.10)`,
            }}
          >
            {link}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};

/** Three real outputs, side by side, with what each one was. */
const Outputs: React.FC<{ unit: number }> = ({ unit: base }) => {
  const unit = base * 1.2;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cards = [
    { file: "out-video.png", label: "Feature launch", note: "video.ai" },
    { file: "out-montage.png", label: "Photo montage", note: "video.ai" },
    { file: "out-quote.png", label: "Customer quote", note: "video.ai" },
  ];
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ display: "flex", gap: unit * 2, alignItems: "flex-end" }}>
        {cards.map((card, index) => {
          const rise = spring({ frame: frame - index * 5, fps, config: { damping: 18 } });
          return (
            <div
              key={card.file}
              style={{
                opacity: rise,
                transform: `translateY(${(1 - rise) * unit * 2}px)`,
                textAlign: "center",
              }}
            >
              <Img
                src={staticFile(card.file)}
                style={{
                  height: unit * 17,
                  borderRadius: unit * 0.8,
                  boxShadow: "0 20px 50px rgba(16,19,34,0.18)",
                  display: "block",
                }}
              />
              <p style={{ fontFamily: FONT, fontSize: unit * 1.15, fontWeight: 650, color: INK, margin: `${unit * 0.8}px 0 0` }}>
                {card.label}
              </p>
              <p style={{ fontFamily: MONO, fontSize: unit * 0.92, color: MUTED, margin: `${unit * 0.2}px 0 0` }}>
                {card.note}
              </p>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};


/**
 * The App Builder: a sentence on the left, a real page on the right.
 *
 * The hardest thing to show about this feature is that the preview is not a
 * mock-up of the app, it is the app, at the address the share link serves. So
 * the page here assembles the way a real build lands: all at once, after the
 * agent has finished, with the deployed badge and the address arriving after
 * it rather than with it.
 */
const AppBuilder: React.FC<{ unit: number }> = ({ unit: base }) => {
  const unit = base * 1.05;
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rise = (delay: number, damping = 18) =>
    spring({ frame: frame - delay, fps, config: { damping } });

  const asked = rise(2);
  const replied = rise(26);
  const built = rise(30, 16);
  const deployed = rise(52, 14);
  const address = rise(60, 15);

  const card = {
    background: "#ffffff",
    borderRadius: unit * 1.1,
    border: "1px solid rgba(16,19,34,0.07)",
  };

  const products = ["Country sourdough", "Honey buns", "Seeded loaf"];

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        // Lifted, because the lower third owns the bottom of the frame and
        // an address sliding underneath it is an address nobody can read.
        paddingBottom: unit * 7,
      }}
    >
      <div
        style={{
          ...card,
          position: "relative",
          width: unit * 58,
          padding: unit * 1.4,
          display: "flex",
          gap: unit * 1.4,
          boxShadow: "0 30px 70px rgba(16,19,34,0.16)",
        }}
      >
        {/* Deployed belongs to the app, so it sits on the app. */}
        <span
          style={{
            position: "absolute",
            top: unit * 2.1,
            right: unit * 2.1,
            zIndex: 2,
            opacity: deployed,
            transform: `translateY(${(1 - deployed) * unit * 0.5}px)`,
            fontFamily: FONT,
            fontSize: unit * 0.85,
            fontWeight: 650,
            color: GOOD,
            background: "rgba(255,255,255,0.94)",
            border: "1px solid rgba(15,157,104,0.35)",
            borderRadius: 999,
            padding: `${unit * 0.3}px ${unit * 0.8}px`,
          }}
        >
          Deployed v1
        </span>
        {/* The conversation. Two bubbles: what they asked, what it says back. */}
        <div
          style={{
            width: unit * 20,
            display: "flex",
            flexDirection: "column",
            gap: unit * 0.9,
          }}
        >
          <div
            style={{
              opacity: asked,
              transform: `translateY(${(1 - asked) * unit}px)`,
              alignSelf: "flex-end",
              maxWidth: "92%",
              background: "rgba(109,74,255,0.12)",
              borderRadius: `${unit * 0.9}px ${unit * 0.9}px ${unit * 0.25}px ${unit * 0.9}px`,
              padding: `${unit * 0.7}px ${unit * 0.85}px`,
              fontFamily: FONT,
              fontSize: unit * 0.92,
              lineHeight: 1.45,
              color: INK,
            }}
          >
            A landing page for a bakery called Sunrise, with the menu and the opening hours
          </div>
          <div
            style={{
              opacity: replied,
              transform: `translateY(${(1 - replied) * unit}px)`,
              alignSelf: "flex-start",
              maxWidth: "92%",
              background: "#f3f5fa",
              borderRadius: `${unit * 0.9}px ${unit * 0.9}px ${unit * 0.9}px ${unit * 0.25}px`,
              padding: `${unit * 0.7}px ${unit * 0.85}px`,
              fontFamily: FONT,
              fontSize: unit * 0.92,
              lineHeight: 1.45,
              color: MUTED,
            }}
          >
            Built the page: header, three products with prices, and the hours.
            <span style={{ fontFamily: MONO, color: BRAND, marginLeft: unit * 0.4 }}>v1</span>
          </div>

          {/* The box they type the next change into. The conversation does not
              end at the first build, and an empty column would say it did. */}
          <div
            style={{
              opacity: rise(64) * 0.9,
              marginTop: "auto",
              border: "1px solid rgba(16,19,34,0.12)",
              borderRadius: unit * 0.7,
              padding: `${unit * 0.6}px ${unit * 0.8}px`,
              fontFamily: FONT,
              fontSize: unit * 0.88,
              color: "#9aa1b5",
            }}
          >
            Add a photo of the shop and a phone number
          </div>
        </div>

        {/* The app itself, at its own address. */}
        <div
          style={{
            flex: 1,
            borderRadius: unit * 0.9,
            overflow: "hidden",
            border: "1px solid rgba(16,19,34,0.08)",
            background: "#fffaf2",
            minHeight: unit * 21,
            opacity: built,
            transform: `scale(${0.985 + built * 0.015})`,
            transformOrigin: "center",
            padding: unit * 1.4,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: unit * 0.5,
          }}
        >
          <p
            style={{
              fontFamily: FONT,
              fontSize: unit * 0.72,
              letterSpacing: unit * 0.12,
              textTransform: "uppercase",
              color: "#c07d2a",
              margin: 0,
            }}
          >
            Bakery and cafe
          </p>
          <p style={{ fontFamily: FONT, fontSize: unit * 2.6, fontWeight: 760, color: "#3b2412", margin: 0 }}>
            Sunrise
          </p>
          <p style={{ fontFamily: FONT, fontSize: unit * 0.95, color: "#7a5a3c", margin: 0 }}>
            Slow sourdough, baked before the sun comes up
          </p>
          <div style={{ display: "flex", gap: unit * 0.7, marginTop: unit * 0.8 }}>
            {products.map((name, index) => {
              const show = rise(34 + index * 4);
              return (
                <div
                  key={name}
                  style={{
                    ...card,
                    opacity: show,
                    transform: `translateY(${(1 - show) * unit * 0.8}px)`,
                    width: unit * 8.4,
                    padding: unit * 0.7,
                  }}
                >
                  <div
                    style={{
                      height: unit * 3,
                      borderRadius: unit * 0.6,
                      background: "linear-gradient(135deg,#ffd88a,#f59435)",
                    }}
                  />
                  <p style={{ fontFamily: FONT, fontSize: unit * 0.78, fontWeight: 650, color: "#3b2412", margin: `${unit * 0.5}px 0 0` }}>
                    {name}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* The address anyone can open. */}
      <div style={{ display: "flex", alignItems: "center", gap: unit * 0.8, marginTop: unit * 1.2 }}>
        <span
          style={{
            opacity: address,
            transform: `translateY(${(1 - address) * unit * 0.6}px)`,
            fontFamily: MONO,
            fontSize: unit * 0.95,
            color: INK,
            background: "#ffffff",
            border: "1px solid rgba(16,19,34,0.1)",
            borderRadius: 999,
            padding: `${unit * 0.35}px ${unit * 0.9}px`,
          }}
        >
          apps.basivo.in/sunrise-bakery-k3d9
        </span>
        <span
          style={{
            opacity: address,
            fontFamily: FONT,
            fontSize: unit * 0.9,
            color: MUTED,
          }}
        >
          send it to anyone
        </span>
      </div>
    </AbsoluteFill>
  );
};

/** What the same three jobs cost you elsewhere, and what they cost here. */
const Compare: React.FC<{ unit: number }> = ({ unit }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const rows = [
    {
      job: "Connect a repository",
      them: "Copy a webhook URL into GitHub, paste a secret, pick events, keep it alive",
      us: "Choose the repository. Basivo registers the webhook and rotates the secret",
    },
    {
      job: "Make a video",
      them: "Stand up a render service, a queue and a bucket beside the automation tool",
      us: "One node on the same canvas. React goes in, an MP4 comes out",
    },
    {
      job: "Know what a run cost",
      them: "Read it off a provider invoice at the end of the month",
      us: "Tokens and dollars on every node, while the run is still going",
    },
  ];
  const head = spring({ frame, fps, config: { damping: 20 } });
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ width: unit * 62 }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: `${unit * 13}px 1fr 1fr`,
            gap: unit * 1.4,
            opacity: head,
            paddingBottom: unit * 0.9,
            borderBottom: "1px solid rgba(16,19,34,0.12)",
          }}
        >
          <span />
          <span style={{ fontFamily: FONT, fontSize: unit * 1.05, fontWeight: 700, letterSpacing: unit * 0.02, color: MUTED }}>
            n8n, Flowise, a render box
          </span>
          <span style={{ fontFamily: FONT, fontSize: unit * 1.05, fontWeight: 700, letterSpacing: unit * 0.02, color: BRAND }}>
            Basivo
          </span>
        </div>
        {rows.map((row, index) => {
          const show = spring({ frame: frame - 8 - index * 7, fps, config: { damping: 20 } });
          return (
            <div
              key={row.job}
              style={{
                display: "grid",
                gridTemplateColumns: `${unit * 13}px 1fr 1fr`,
                gap: unit * 1.4,
                alignItems: "start",
                padding: `${unit * 1.1}px 0`,
                borderBottom: "1px solid rgba(16,19,34,0.08)",
                opacity: show,
                transform: `translateY(${(1 - show) * unit}px)`,
              }}
            >
              <span style={{ fontFamily: FONT, fontSize: unit * 1.2, fontWeight: 650, color: INK }}>
                {row.job}
              </span>
              <span style={{ fontFamily: FONT, fontSize: unit * 1.12, lineHeight: 1.45, color: MUTED }}>
                {row.them}
              </span>
              <span style={{ fontFamily: FONT, fontSize: unit * 1.12, lineHeight: 1.45, color: INK, fontWeight: 560 }}>
                <span style={{ color: GOOD, fontWeight: 700, marginRight: unit * 0.4 }}>+</span>
                {row.us}
              </span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

/** The opening card: the mark, the name, the promise. */
const Opening: React.FC<{ unit: number; tagline: string }> = ({ unit, tagline }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const mark = spring({ frame, fps, config: { damping: 14, mass: 0.8 } });
  const name = spring({ frame: frame - 6, fps, config: { damping: 20 } });
  const line = spring({ frame: frame - 14, fps, config: { damping: 22 } });
  return (
    <AbsoluteFill
      style={{ backgroundColor: "#eef1f7", justifyContent: "center", alignItems: "center" }}
    >
      <div style={{ textAlign: "center" }}>
        <Img
          src={staticFile("mark.png")}
          style={{
            width: unit * 9,
            height: unit * 9,
            borderRadius: unit * 2,
            opacity: mark,
            transform: `scale(${0.7 + mark * 0.3})`,
          }}
        />
        <p
          style={{
            fontFamily: FONT,
            fontSize: unit * 4.4,
            fontWeight: 720,
            color: INK,
            margin: `${unit * 1.4}px 0 0`,
            opacity: name,
            transform: `translateY(${(1 - name) * unit}px)`,
          }}
        >
          Basivo
        </p>
        <p
          style={{
            fontFamily: FONT,
            fontSize: unit * 1.85,
            color: MUTED,
            margin: `${unit * 0.7}px 0 0`,
            opacity: line,
            transform: `translateY(${(1 - line) * unit * 0.8}px)`,
          }}
        >
          {tagline}
        </p>
      </div>
    </AbsoluteFill>
  );
};

/** A rectangle with room around it, for a camera that should not sit tight. */
const grow = (rect: Rect, by: number): Rect => ({
  x: rect.x - (rect.w * (by - 1)) / 2,
  y: rect.y - (rect.h * (by - 1)) / 2,
  w: rect.w * by,
  h: rect.h * by,
});

/** One rectangle covering two, for a shot that has to hold both. */
const span = (a: Rect, b: Rect): Rect => {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return {
    x,
    y,
    w: Math.max(a.x + a.w, b.x + b.w) - x,
    h: Math.max(a.y + a.h, b.y + b.h) - y,
  };
};

/** Two numbers, large, because they are the argument. */
const Receipt: React.FC<{ unit: number }> = ({ unit }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const seconds = interpolate(frame, [6, 34], [0, 36], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: EASE,
  });
  const cents = interpolate(frame, [10, 38], [0, 9], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: EASE,
  });
  const rise = spring({ frame, fps, config: { damping: 20 } });
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
      <div style={{ textAlign: "center", opacity: rise, transform: `translateY(${(1 - rise) * unit}px)` }}>
        <p
          style={{
            margin: 0,
            fontFamily: FONT,
            fontSize: unit * 1.15,
            letterSpacing: unit * 0.16,
            textTransform: "uppercase",
            color: BRAND,
            fontWeight: 700,
          }}
        >
          One real run
        </p>
        <div style={{ display: "flex", gap: unit * 6, marginTop: unit * 2 }}>
          {[
            { value: `${Math.round(seconds)}s`, label: "start to pull request" },
            { value: `$0.0${Math.round(cents)}`, label: "of your own model spend" },
          ].map((item) => (
            <div key={item.label}>
              <p
                style={{
                  margin: 0,
                  fontFamily: FONT,
                  fontSize: unit * 7.5,
                  fontWeight: 720,
                  color: INK,
                  lineHeight: 1,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {item.value}
              </p>
              <p
                style={{
                  margin: `${unit * 0.7}px 0 0`,
                  fontFamily: FONT,
                  fontSize: unit * 1.2,
                  color: MUTED,
                }}
              >
                {item.label}
              </p>
            </div>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};

