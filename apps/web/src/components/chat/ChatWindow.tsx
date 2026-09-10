/**
 * The chat window a Chat trigger publishes.
 *
 * One component, used twice: on the public page a visitor opens, and inside
 * the builder so the person who drew the flow can talk to it without leaving
 * the canvas. Both talk to the same three unauthenticated endpoints, so what
 * is tested in the builder is exactly what a customer gets.
 *
 * Two things separate this from a chat box over an API. While an answer is
 * being made the window shows what the flow is doing — which node is running,
 * which model is thinking, which tools it called — because an agent can take
 * twenty seconds and a blank window for twenty seconds reads as broken. Once
 * the answer lands those steps stay behind a disclosure under it, so "why did
 * it say that" has an answer that does not need the run log. The flow's author
 * can switch both off for a chat given to customers.
 *
 * It polls rather than streams. The endpoints are public, and a stream held
 * open per visitor is a cheap way for a stranger to occupy the server; the
 * interval backs off as a run gets long, so a two minute answer costs a
 * handful of requests rather than a hundred.
 *
 * The session id lives in this browser and nowhere else. It is what the
 * agent's memory keys on, so a reload continues the conversation and two
 * visitors never share one.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { API_BASE } from "../../lib/api";

type Role = "you" | "them";

export type ChatAttachment = {
  url: string;
  kind: "image" | "video" | "audio" | "file";
  filename: string;
  content_type: string;
  size_bytes: number;
};

export type ChatStep = {
  label: string;
  kind: string;
  status: string;
  duration_ms: number | null;
  detail: string;
};

type Message = {
  id: string;
  role: Role;
  text: string;
  steps?: ChatStep[];
  files?: ChatAttachment[];
};

type Window = {
  title: string;
  greeting: string;
  placeholder: string;
  suggestions: string[];
  show_activity: boolean;
};

/** How long to wait before asking again, given how long we have waited. */
function nextDelay(elapsedMs: number): number {
  if (elapsedMs < 5_000) return 700;
  if (elapsedMs < 20_000) return 1_500;
  return 3_000;
}

/** Give up on one answer. A flow can legitimately take a while; not this long. */
const ANSWER_TIMEOUT_MS = 180_000;

const STORAGE_KEY = "basivo.chat.session";

function sessionId(flowId: string): string {
  const key = `${STORAGE_KEY}.${flowId}`;
  try {
    const held = window.localStorage.getItem(key);
    if (held) return held;
    const made = crypto.randomUUID();
    window.localStorage.setItem(key, made);
    return made;
  } catch {
    // Private windows and blocked storage: a session that lasts as long as
    // this tab is better than refusing to chat.
    return crypto.randomUUID();
  }
}

export function ChatWindow({
  flowId,
  token,
  className = "",
  renameDocument = false,
}: {
  flowId: string;
  token: string;
  className?: string;
  /** On the page a visitor opens, the tab should say whose chat this is. */
  renameDocument?: boolean;
}) {
  const [window_, setWindow] = useState<Window | null>(null);
  const [gone, setGone] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [live, setLive] = useState<ChatStep[]>([]);
  const [failure, setFailure] = useState("");
  const session = useRef<string>("");
  const bottom = useRef<HTMLDivElement | null>(null);
  const base = `${API_BASE}/chat/${flowId}/${token}`;

  if (!session.current) session.current = sessionId(flowId);

  useEffect(() => {
    let alive = true;
    void fetch(base)
      .then((response) =>
        response.ok ? response.json() : Promise.reject(response.status),
      )
      .then((drawn: Window) => alive && setWindow(drawn))
      .catch(() => alive && setGone(true));
    return () => {
      alive = false;
    };
  }, [base]);

  useEffect(() => {
    if (!renameDocument || !window_) return;
    document.title = window_.title;
  }, [renameDocument, window_]);

  // Follow the conversation down, but only for new messages: a scroll on
  // every render fights the reader when they look back at an earlier answer.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, waiting, live.length]);

  const ask = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || waiting) return;
      setFailure("");
      setDraft("");
      setLive([]);
      setMessages((all) => [
        ...all,
        { id: crypto.randomUUID(), role: "you", text: trimmed },
      ]);
      setWaiting(true);

      try {
        const started = await fetch(base, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: trimmed, session_id: session.current }),
        });
        if (!started.ok) throw new Error(String(started.status));
        const { run_id: runId } = (await started.json()) as { run_id: string };

        const began = Date.now();
        for (;;) {
          await new Promise((resume) =>
            setTimeout(resume, nextDelay(Date.now() - began)),
          );
          if (Date.now() - began > ANSWER_TIMEOUT_MS) throw new Error("timeout");

          const polled = await fetch(`${base}/${runId}`);
          if (!polled.ok) throw new Error(String(polled.status));
          const state = (await polled.json()) as {
            status: string;
            reply: string;
            error: string | null;
            steps: ChatStep[];
            attachments: ChatAttachment[];
          };
          setLive(state.steps ?? []);

          if (state.status === "succeeded") {
            setMessages((all) => [
              ...all,
              {
                id: runId,
                role: "them",
                text: state.reply,
                steps: state.steps ?? [],
                files: state.attachments ?? [],
              },
            ]);
            return;
          }
          if (state.status === "failed" || state.status === "cancelled") {
            throw new Error(state.error ?? "failed");
          }
        }
      } catch {
        setFailure("That did not go through. Try again in a moment.");
      } finally {
        setWaiting(false);
        setLive([]);
      }
    },
    [base, waiting],
  );

  if (gone) {
    return (
      <div
        className={`surface grid place-items-center rounded-2xl p-10 ${className}`}
      >
        <p className="text-sm text-ink-400">
          This chat is not available. The link may have been turned off.
        </p>
      </div>
    );
  }

  if (!window_) {
    return (
      <div
        className={`surface grid place-items-center rounded-2xl p-10 ${className}`}
      >
        <p className="text-sm text-ink-500">Opening…</p>
      </div>
    );
  }

  const fresh = messages.length === 0;

  return (
    <div
      className={`surface flex flex-col overflow-hidden rounded-2xl ${className}`}
    >
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-5 sm:px-6">
        {fresh ? (
          // An empty chat is the hardest screen in any chat product: the
          // greeting is the only thing telling somebody what to type, so on
          // an empty window it is the page rather than a first bubble.
          <div className="mx-auto max-w-md py-10 text-center">
            <h2 className="text-lg font-semibold text-ink-100">
              {window_.title}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-ink-400">
              {window_.greeting}
            </p>
          </div>
        ) : (
          <Bubble role="them" text={window_.greeting} />
        )}
        {messages.map((message) => (
          <Bubble
            key={message.id}
            role={message.role}
            text={message.text}
            steps={message.steps}
            files={message.files}
            showSteps={window_.show_activity}
          />
        ))}
        {waiting && <Working steps={window_.show_activity ? live : []} />}
        {failure && (
          <p className="text-xs" style={{ color: "var(--status-bad)" }}>
            {failure}
          </p>
        )}
        <div ref={bottom} />
      </div>

      {fresh && window_.suggestions.length > 0 && (
        <div className="flex flex-wrap justify-center gap-2 px-4 pb-3 sm:px-6">
          {window_.suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => void ask(suggestion)}
              className="rounded-full border border-[var(--edge-strong)] px-3.5 py-2 text-xs text-ink-300 transition-colors hover:border-brand-400 hover:text-ink-100"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <form
        className="flex items-end gap-2 border-t border-[var(--edge)] p-3 sm:px-6 sm:py-4"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(draft);
        }}
      >
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter writes a new line. The other way round
            // is what every other chat does, and getting it wrong sends half
            // a sentence.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void ask(draft);
            }
          }}
          rows={1}
          placeholder={window_.placeholder}
          aria-label="Message"
          className="max-h-40 min-h-[2.75rem] flex-1 resize-y rounded-xl border border-[var(--edge-strong)] bg-ink-950/40 px-3.5 py-2.5 text-sm text-ink-100 outline-none placeholder:text-ink-500 focus:border-brand-400"
        />
        <button
          type="submit"
          disabled={waiting || !draft.trim()}
          className="rounded-xl bg-brand-500 px-4 py-2.5 text-sm font-medium text-white transition-opacity disabled:opacity-40"
        >
          Send
        </button>
      </form>
    </div>
  );
}

function Bubble({
  role,
  text,
  steps,
  files,
  showSteps = false,
}: {
  role: Role;
  text: string;
  steps?: ChatStep[];
  files?: ChatAttachment[];
  showSteps?: boolean;
}) {
  const mine = role === "you";
  return (
    <div
      className={mine ? "flex flex-col items-end" : "flex flex-col items-start"}
    >
      {(text || !files?.length) && (
        <div
          className={`max-w-[85%] space-y-1.5 rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
            mine
              ? "bg-brand-500 text-white"
              : "border border-[var(--edge)] bg-ink-950/40 text-ink-200"
          }`}
        >
          {mine ? (
            <p className="whitespace-pre-wrap">{text}</p>
          ) : (
            <Formatted text={text} />
          )}
        </div>
      )}
      {files?.map((file) => (
        <Attachment key={file.url} file={file} />
      ))}
      {!mine && showSteps && steps && steps.length > 0 && (
        <Steps steps={steps} />
      )}
    </div>
  );
}

/**
 * A file the flow made, shown rather than linked.
 *
 * The whole point of a video node answering a chat is that the person sees the
 * video. A link called `promo.mp4` is a download somebody opens later, if at
 * all. Everything is served from the chat's own path, so nothing here needs an
 * account and nothing else in the workspace is reachable through it.
 */
function Attachment({ file }: { file: ChatAttachment }) {
  const url = `${API_BASE}${file.url}`;
  const frame =
    "mt-1.5 max-w-[85%] overflow-hidden rounded-2xl border border-[var(--edge)]";

  if (file.kind === "video") {
    return (
      <div className={frame}>
        <video
          src={url}
          controls
          playsInline
          preload="metadata"
          className="block max-h-[26rem] w-full bg-black"
        />
      </div>
    );
  }
  if (file.kind === "image") {
    return (
      <a href={url} target="_blank" rel="noreferrer" className={frame}>
        <img
          src={url}
          alt={file.filename}
          loading="lazy"
          className="block max-h-[26rem] w-full object-contain"
        />
      </a>
    );
  }
  if (file.kind === "audio") {
    return (
      <div className={`${frame} bg-ink-950/40 p-2.5`}>
        <audio src={url} controls preload="metadata" className="w-72 max-w-full" />
      </div>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="mt-1.5 flex max-w-[85%] items-center gap-2 rounded-xl border border-[var(--edge)] px-3 py-2 text-xs text-ink-300 transition-colors hover:border-brand-400"
    >
      {file.filename}
      <span className="text-ink-500">{formatBytes(file.size_bytes)}</span>
    </a>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** What ran, under a finished answer. Closed until somebody wants it. */
function Steps({ steps }: { steps: ChatStep[] }) {
  const [open, setOpen] = useState(false);
  const total = steps.reduce((sum, step) => sum + (step.duration_ms ?? 0), 0);

  return (
    <div className="mt-1.5 w-full max-w-[85%]">
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-lg px-1.5 py-1 text-xs text-ink-500 transition-colors hover:text-ink-300"
      >
        <svg
          viewBox="0 0 24 24"
          className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
        {steps.length} {steps.length === 1 ? "step" : "steps"}
        {total > 0 && ` · ${formatMs(total)}`}
      </button>
      {open && <StepList steps={steps} />}
    </div>
  );
}

/** The same list, live, while the answer is still being made. */
function Working({ steps }: { steps: ChatStep[] }) {
  const latest = steps[steps.length - 1];
  return (
    <div className="flex w-full flex-col items-start gap-1.5">
      <div className="flex items-center gap-2.5 rounded-2xl border border-[var(--edge)] bg-ink-950/40 px-4 py-3">
        <span className="flex gap-1">
          {[0, 1, 2].map((dot) => (
            <span
              key={dot}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-400"
              style={{ animationDelay: `${dot * 0.15}s` }}
            />
          ))}
        </span>
        {latest && (
          <span className="text-xs text-ink-400">
            {latest.label}
            {latest.detail ? ` · ${latest.detail}` : ""}
          </span>
        )}
      </div>
      {steps.length > 1 && (
        <div className="w-full max-w-[85%]">
          <StepList steps={steps} />
        </div>
      )}
    </div>
  );
}

function StepList({ steps }: { steps: ChatStep[] }) {
  return (
    <ol className="mt-1 space-y-1.5 rounded-xl border border-[var(--edge)] bg-ink-950/40 p-3">
      {steps.map((step, index) => (
        <li key={index} className="flex items-baseline gap-2 text-xs">
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: tone(step.status) }}
          />
          <span className="shrink-0 text-ink-200">{step.label}</span>
          <span className="shrink-0 text-ink-500">{step.kind}</span>
          {step.detail && (
            <span className="truncate text-ink-400" title={step.detail}>
              {step.detail}
            </span>
          )}
          <span className="ml-auto shrink-0 font-mono text-ink-500">
            {step.duration_ms != null ? formatMs(step.duration_ms) : "…"}
          </span>
        </li>
      ))}
    </ol>
  );
}

function tone(status: string): string {
  if (status === "succeeded") return "var(--status-good)";
  if (status === "failed") return "var(--status-bad)";
  return "var(--status-warn)";
}

function formatMs(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Just enough markdown for what a model actually writes back.
 *
 * Models answer in markdown whether or not anyone asked, so a plain bubble
 * shows a customer `**Nemotron**` and a wall of asterisks. This handles the
 * three things that turn up in nearly every reply — bold, inline code and
 * dash bullets — and leaves everything else as typed.
 *
 * It builds React elements rather than HTML. The text comes from a model,
 * which got it from whatever the flow read, so it is never trusted enough to
 * be set as markup.
 */
function Formatted({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];

  const flush = () => {
    if (bullets.length === 0) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="list-disc space-y-1 pl-4">
        {bullets.map((item, index) => (
          <li key={index}>{inline(item)}</li>
        ))}
      </ul>,
    );
    bullets = [];
  };

  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      bullets.push(bullet[1]);
      continue;
    }
    flush();
    if (line.trim()) {
      blocks.push(
        <p key={`p-${blocks.length}`} className="whitespace-pre-wrap">
          {inline(line)}
        </p>,
      );
    }
  }
  flush();

  return <>{blocks}</>;
}

/** Bold and inline code within one line. */
function inline(line: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(line))) {
    if (match.index > last) parts.push(line.slice(last, match.index));
    if (match[1] !== undefined) {
      parts.push(
        <strong key={parts.length} className="font-semibold">
          {match[1]}
        </strong>,
      );
    } else {
      parts.push(
        <code
          key={parts.length}
          className="rounded bg-ink-800/60 px-1 py-0.5 font-mono text-[0.8em]"
        >
          {match[2]}
        </code>,
      );
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}
