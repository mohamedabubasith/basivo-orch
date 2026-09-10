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

/**
 * The backstop, not the expectation.
 *
 * A flow that renders a video can legitimately take five minutes: the model
 * writes a storyboard, writes a composition, gets told the composition is
 * wrong, writes it again, and only then does a browser draw 150 frames. This
 * used to stop watching after three minutes and tell the visitor it had not
 * gone through, while the run went on to finish and save the video. So the
 * window follows the run for as long as the server says it is running, and
 * this number exists only so an abandoned tab stops polling eventually.
 */
const GIVE_UP_MS = 20 * 60_000;
/** After this long, say so. Silence and a spinner is how a page reads as broken. */
const SLOW_MS = 45_000;
/** A poll can fail because the API is restarting. That is not the answer failing. */
const POLL_FAILURES_ALLOWED = 5;

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
  const [slow, setSlow] = useState(false);
  //: Seconds since the message was sent. A window that says "still running"
  //: for two minutes with no number reads as stuck; the same window with a
  //: clock on it reads as working, which is the truth.
  const [elapsed, setElapsed] = useState(0);
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

  useEffect(() => {
    if (!waiting) return;
    const started = Date.now();
    setElapsed(0);
    const tick = setInterval(
      () => setElapsed(Math.round((Date.now() - started) / 1000)),
      1000,
    );
    return () => clearInterval(tick);
  }, [waiting]);

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
      setSlow(false);
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
        let missed = 0;
        for (;;) {
          const waited = Date.now() - began;
          await new Promise((resume) => setTimeout(resume, nextDelay(waited)));
          if (waited > SLOW_MS) setSlow(true);
          if (waited > GIVE_UP_MS) {
            setFailure(
              "This is still running after twenty minutes, so the window has stopped " +
                "watching it. The answer is on the run that was started.",
            );
            return;
          }

          const polled = await fetch(`${base}/${runId}`).catch(() => null);
          if (!polled || !polled.ok) {
            // A restarting API answers 502 for a few seconds. Losing an answer
            // that is still being made over that would be absurd.
            missed += 1;
            if (missed > POLL_FAILURES_ALLOWED) throw new Error("unreachable");
            continue;
          }
          missed = 0;
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
            // The flow's own words, not ours: the server has already decided
            // what a stranger may be told about why it failed.
            setFailure(state.error ?? "Something went wrong answering that.");
            return;
          }
        }
      } catch {
        setFailure("The server could not be reached. Try again in a moment.");
      } finally {
        setWaiting(false);
        setLive([]);
        setSlow(false);
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
        {waiting && (
          <Working
            steps={window_.show_activity ? live : []}
            slow={slow}
            elapsed={elapsed}
          />
        )}
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
function Working({
  steps,
  slow = false,
  elapsed = 0,
}: {
  steps: ChatStep[];
  slow?: boolean;
  elapsed?: number;
}) {
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
        <span className="text-xs text-ink-400">
          {latest ? latest.label : "Working"}
          {elapsed > 2 && ` · ${clock(elapsed)}`}
          {latest?.detail ? ` · ${latest.detail}` : ""}
        </span>
      </div>
      {slow && (
        <p className="px-1 text-xs text-ink-500">
          {latest ? `${latest.label} is still running. ` : "Still working. "}
          Some steps take minutes; a slow model is the usual reason. The answer
          appears here when it is done.
        </p>
      )}
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

/** Seconds as a person counts them. */
function clock(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
}

/**
 * Enough markdown for what a model actually writes back.
 *
 * Models answer in markdown whether or not anyone asked, and a chat window
 * that prints it raw shows a customer `**Nemotron**`, a wall of asterisks and,
 * worst of all, a table as a screenful of pipes and dashes. This handles what
 * turns up in practice: headings, bold, inline code, links, dash and numbered
 * lists, quotes, fenced code, and tables.
 *
 * It builds React elements rather than HTML. The text comes from a model,
 * which got it from whatever the flow read, so it is never trusted enough to
 * be set as markup — and a link's href is checked before it is used, because
 * `javascript:` is a link too.
 */
function Formatted({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let index = 0;

  const flushList = (items: string[], ordered: boolean) => {
    if (items.length === 0) return;
    const Tag = ordered ? "ol" : "ul";
    blocks.push(
      <Tag
        key={`list-${blocks.length}`}
        className={`space-y-1 pl-5 ${ordered ? "list-decimal" : "list-disc"}`}
      >
        {items.map((item, at) => (
          <li key={at}>{inline(item)}</li>
        ))}
      </Tag>,
    );
  };

  while (index < lines.length) {
    const line = lines[index];

    // A fenced block: kept verbatim, because that is the point of a fence.
    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        <pre
          key={`code-${blocks.length}`}
          className="overflow-x-auto rounded-xl border border-[var(--edge)] bg-ink-950/60 p-3 font-mono text-xs"
        >
          {body.join("\n")}
        </pre>,
      );
      continue;
    }

    // A table: header, a row of dashes, then rows. Anything else that starts
    // with a pipe is treated as text rather than guessed at.
    if (isRow(line) && isDivider(lines[index + 1] ?? "")) {
      const header = cells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && isRow(lines[index])) {
        rows.push(cells(lines[index]));
        index += 1;
      }
      blocks.push(
        <div
          key={`table-${blocks.length}`}
          className="overflow-x-auto rounded-xl border border-[var(--edge)]"
        >
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-[var(--edge)]">
                {header.map((cell, at) => (
                  <th key={at} className="px-2.5 py-2 font-semibold text-ink-200">
                    {inline(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, at) => (
                <tr key={at} className="border-b border-[var(--edge)] last:border-0">
                  {row.map((cell, cellAt) => (
                    <td key={cellAt} className="px-2.5 py-2 align-top text-ink-300">
                      {inline(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      const items: string[] = [];
      while (index < lines.length) {
        const next = lines[index].match(
          ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*]\s+(.*)$/,
        );
        if (!next) break;
        items.push(next[1]);
        index += 1;
      }
      flushList(items, ordered);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      blocks.push(
        <p
          key={`h-${blocks.length}`}
          className="pt-1 text-sm font-semibold text-ink-100"
        >
          {inline(heading[2])}
        </p>,
      );
      index += 1;
      continue;
    }

    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      blocks.push(
        <p
          key={`q-${blocks.length}`}
          className="border-l-2 border-[var(--edge-strong)] pl-3 text-ink-400"
        >
          {inline(quote[1])}
        </p>,
      );
      index += 1;
      continue;
    }

    if (line.trim()) {
      blocks.push(
        <p key={`p-${blocks.length}`} className="whitespace-pre-wrap">
          {inline(line)}
        </p>,
      );
    }
    index += 1;
  }

  return <>{blocks}</>;
}

const isRow = (line: string): boolean => line.trim().startsWith("|");
const isDivider = (line: string): boolean =>
  /^\s*\|[\s:|-]+\|?\s*$/.test(line) && line.includes("-");

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/** A link we are willing to open. Anything else stays as its own text. */
function safeHref(href: string): string | null {
  const trimmed = href.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : null;
}

/** Bold, inline code and links within one line. */
function inline(line: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern =
    /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s)]+)/g;
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
    } else if (match[2] !== undefined) {
      parts.push(
        <code
          key={parts.length}
          className="rounded bg-ink-800/60 px-1 py-0.5 font-mono text-[0.85em]"
        >
          {match[2]}
        </code>,
      );
    } else if (match[3] !== undefined) {
      const href = safeHref(match[4]);
      parts.push(
        href ? (
          <a
            key={parts.length}
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="text-brand-400 underline decoration-dotted underline-offset-2"
          >
            {match[3]}
          </a>
        ) : (
          match[3]
        ),
      );
    } else if (match[5] !== undefined) {
      const href = safeHref(match[5]);
      parts.push(
        href ? (
          <a
            key={parts.length}
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="break-all text-brand-400 underline decoration-dotted underline-offset-2"
          >
            {match[5]}
          </a>
        ) : (
          match[5]
        ),
      );
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}
