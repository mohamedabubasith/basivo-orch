/**
 * The chat window a Chat trigger publishes.
 *
 * One component, used twice: on the public page a visitor opens, and inside
 * the builder so the person who drew the flow can talk to it without leaving
 * the canvas. Both talk to the same three unauthenticated endpoints, so what
 * is tested in the builder is exactly what a customer gets.
 *
 * It polls rather than streams. The endpoints are public, and a stream held
 * open per visitor is a cheap way for a stranger to occupy the server; a reply
 * takes seconds. The interval backs off as a run gets long, so a two minute
 * answer costs a handful of requests rather than a hundred.
 *
 * The session id lives in this browser and nowhere else. It is what the
 * agent's memory keys on, so a reload continues the conversation and two
 * visitors never share one.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { API_BASE } from "../../lib/api";

type Role = "you" | "them";

type Message = {
  id: string;
  role: Role;
  text: string;
};

type Window = {
  title: string;
  greeting: string;
  placeholder: string;
  suggestions: string[];
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
  const [failure, setFailure] = useState("");
  const session = useRef<string>("");
  const bottom = useRef<HTMLDivElement | null>(null);
  const base = `${API_BASE}/chat/${flowId}/${token}`;

  if (!session.current) session.current = sessionId(flowId);

  useEffect(() => {
    let live = true;
    void fetch(base)
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((drawn: Window) => live && setWindow(drawn))
      .catch(() => live && setGone(true));
    return () => {
      live = false;
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
  }, [messages.length, waiting]);

  const ask = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || waiting) return;
      setFailure("");
      setDraft("");
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
          if (Date.now() - began > ANSWER_TIMEOUT_MS) {
            throw new Error("timeout");
          }
          const polled = await fetch(`${base}/${runId}`);
          if (!polled.ok) throw new Error(String(polled.status));
          const state = (await polled.json()) as {
            status: string;
            reply: string;
            error: string | null;
          };
          if (state.status === "succeeded") {
            setMessages((all) => [
              ...all,
              { id: runId, role: "them", text: state.reply },
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
      }
    },
    [base, waiting],
  );

  if (gone) {
    return (
      <div className={`surface grid place-items-center rounded-2xl p-10 ${className}`}>
        <p className="text-sm text-ink-400">
          This chat is not available. The link may have been turned off.
        </p>
      </div>
    );
  }

  if (!window_) {
    return (
      <div className={`surface grid place-items-center rounded-2xl p-10 ${className}`}>
        <p className="text-sm text-ink-500">Opening…</p>
      </div>
    );
  }

  const fresh = messages.length === 0;

  return (
    <div className={`surface flex flex-col overflow-hidden rounded-2xl ${className}`}>
      <header className="flex items-center gap-2.5 border-b border-[var(--edge)] px-4 py-3">
        <span
          aria-hidden="true"
          className="h-2 w-2 rounded-full"
          style={{ background: "var(--status-good)" }}
        />
        <h2 className="text-sm font-semibold text-ink-100">{window_.title}</h2>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        <Bubble role="them" text={window_.greeting} />
        {messages.map((message) => (
          <Bubble key={message.id} role={message.role} text={message.text} />
        ))}
        {waiting && <Typing />}
        {failure && (
          <p className="text-xs" style={{ color: "var(--status-bad)" }}>
            {failure}
          </p>
        )}
        <div ref={bottom} />
      </div>

      {fresh && window_.suggestions.length > 0 && (
        <div className="flex flex-wrap gap-2 px-4 pb-3">
          {window_.suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => void ask(suggestion)}
              className="rounded-full border border-[var(--edge-strong)] px-3 py-1.5 text-xs text-ink-300 transition-colors hover:border-brand-400 hover:text-ink-100"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <form
        className="flex items-end gap-2 border-t border-[var(--edge)] p-3"
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
          className="max-h-32 min-h-[2.5rem] flex-1 resize-y rounded-xl border border-[var(--edge-strong)] bg-ink-950/40 px-3 py-2 text-sm text-ink-100 outline-none placeholder:text-ink-500 focus:border-brand-400"
        />
        <button
          type="submit"
          disabled={waiting || !draft.trim()}
          className="rounded-xl bg-brand-500 px-3.5 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-40"
        >
          Send
        </button>
      </form>
    </div>
  );
}

function Bubble({ role, text }: { role: Role; text: string }) {
  const mine = role === "you";
  return (
    <div className={mine ? "flex justify-end" : "flex justify-start"}>
      <div
        className={`max-w-[85%] space-y-1.5 rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${
          mine
            ? "bg-brand-500 text-white"
            : "border border-[var(--edge)] bg-ink-950/40 text-ink-200"
        }`}
      >
        {mine ? <p className="whitespace-pre-wrap">{text}</p> : <Formatted text={text} />}
      </div>
    </div>
  );
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
        <code key={parts.length} className="rounded bg-ink-800/60 px-1 py-0.5 font-mono text-[0.8em]">
          {match[2]}
        </code>,
      );
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}

/** Three dots, so a slow answer looks like thinking rather than a dead page. */
function Typing() {
  return (
    <div className="flex justify-start">
      <span className="flex gap-1 rounded-2xl border border-[var(--edge)] bg-ink-950/40 px-3.5 py-3">
        {[0, 1, 2].map((dot) => (
          <span
            key={dot}
            className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-400"
            style={{ animationDelay: `${dot * 0.15}s` }}
          />
        ))}
      </span>
    </div>
  );
}
