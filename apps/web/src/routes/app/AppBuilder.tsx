/**
 * One app: what you asked for on the left, what exists on the right.
 *
 * Three things about this screen are deliberate.
 *
 * **The preview is the point.** It takes the larger half, it reloads the
 * moment a message finishes, and it shows a real build at a real address
 * rather than a rendering of one. What you are looking at is exactly what the
 * share link serves.
 *
 * **Waiting is honest.** A turn is a coding agent doing real work, so it takes
 * a minute or two. The panel says which step it is on and how long it has been
 * going, because a spinner with no elapsed time is how people conclude that
 * something is broken and press the button again.
 *
 * **A failed message is not a broken app.** The project keeps the last version
 * that built, so the preview keeps working while the chat explains what went
 * wrong. That is why failures appear as a line in the conversation rather than
 * as a screen full of red.
 */

import { motion } from "motion/react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Link, useParams } from "react-router-dom";

import { API_BASE, ApiError, api, isSessionEnded } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import { Alert, Button, Pill, Spinner } from "../../components/ui";
import { Formatted } from "../../components/chat/markdown";
import { CodeBrowser, type SourceFile } from "./code";
import { RelativeTime } from "./bits";
import type { AppProject } from "./Apps";

interface Turn {
  id: string;
  prompt: string;
  reply: string;
  error: string;
  status: "queued" | "running" | "built" | "failed";
  run_id: string | null;
  version: number | null;
  created_at: string;
}

interface Asset {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  /** What the page refers to it by: `/uploads/logo.png`. */
  path: string;
  /** Where the console loads it from, which works before anything is built. */
  url: string;
  created_at: string;
}

interface Library {
  items: Asset[];
  used_bytes: number;
  limit_bytes: number;
  workspace_used_bytes: number;
  workspace_limit_bytes: number | null;
}

interface Version {
  id: string;
  version: number;
  engine: string;
  created_at: string;
  url: string;
  published: boolean;
}

/** While a turn is running, ask this often. Turns take minutes, not seconds. */
const POLL_MS = 2500;

/** Bytes as a person reads them: "1.4 MB", "820 KB". */
function size(bytes: number): string {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export default function AppBuilder() {
  const { appId } = useParams();
  const { orgId } = useWorkspace();

  const [project, setProject] = useState<AppProject | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [previewKey, setPreviewKey] = useState(0);
  const [copied, setCopied] = useState(false);
  const [library, setLibrary] = useState<Library | null>(null);
  const [uploading, setUploading] = useState(false);
  // What the agent is doing right now, straight from the run's own events.
  const [activity, setActivity] = useState("");
  const [tab, setTab] = useState<"preview" | "code">("preview");
  const [files, setFiles] = useState<SourceFile[] | null>(null);
  const [history, setHistory] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const base = orgId ? `/api/v1/orgs/${orgId}/apps/${appId}` : "";
  const running = turns.some(
    (turn) => turn.status === "queued" || turn.status === "running",
  );
  const builtCount = versions.length;
  /** The version the preview and the code tab show: the newest one. */
  const latestId = versions[0]?.id ?? "";

  const load = useCallback(async () => {
    if (!base) return;
    try {
      const [one, two, three, four] = await Promise.all([
        api.get<AppProject>(base),
        api.get<Turn[]>(`${base}/turns`),
        api.get<Version[]>(`${base}/versions`),
        api.get<Library>(`${base}/assets`),
      ]);
      setProject(one);
      setTurns(two);
      setVersions(three);
      setLibrary(four);
      setError("");
      return three.length;
    } catch (err) {
      if (isSessionEnded(err)) return;
      setError("Could not load this app.");
    }
  }, [base]);

  useEffect(() => {
    void load();
  }, [load]);

  const runId =
    turns.find((turn) => turn.status === "queued" || turn.status === "running")
      ?.run_id ?? null;

  // While a turn runs, poll two things: the project, because a finished turn
  // is a new version and a stale preview, and the run's own event log, which
  // is where the agent says what it is doing. The second is why the wait shows
  // "Editing Menu.tsx" rather than a spinner and a guess.
  useEffect(() => {
    if (!running) {
      setActivity("");
      return;
    }
    let live = true;
    let after = 0;
    const tick = async () => {
      if (!live) return;
      const count = await load();
      if (typeof count === "number" && count !== builtCount) {
        setPreviewKey((key) => key + 1);
      }
      if (!runId || !orgId) return;
      try {
        const log = await api.get<{
          events: { seq: number; type: string; data: Record<string, unknown> }[];
          next_after: number;
        }>(`/api/v1/orgs/${orgId}/runs/${runId}/events?after=${after}`);
        after = log.next_after;
        const said = log.events
          .filter(
            (event) =>
              event.type === "node.progress" &&
              typeof event.data?.progress === "string",
          )
          .pop();
        if (said) setActivity(String(said.data.progress));
      } catch {
        // The clock beside the spinner is still honest without this.
      }
    };
    const timer = window.setInterval(tick, POLL_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [running, load, builtCount, runId, orgId]);

  // The code tab reads a version, so it is fetched when that tab is open and
  // again whenever a turn produces a new one. Not on page load: most visits
  // are to the preview, and the source is the larger of the two.
  useEffect(() => {
    if (tab !== "code" || !base || !latestId) {
      return;
    }
    let live = true;
    void (async () => {
      try {
        const list = await api.get<SourceFile[]>(
          `${base}/versions/${latestId}/files`,
        );
        if (live) setFiles(list);
      } catch (err) {
        if (!isSessionEnded(err) && live) setFiles([]);
      }
    })();
    return () => {
      live = false;
    };
  }, [tab, base, latestId]);

  async function send(event: FormEvent) {
    event.preventDefault();
    const text = message.trim();
    if (!base || !text || running) return;
    setSending(true);
    try {
      await api.post(`${base}/messages`, { text });
      setMessage("");
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "That message could not be sent.",
      );
    } finally {
      setSending(false);
    }
  }

  async function act(path: string) {
    if (!base) return;
    try {
      await api.post(`${base}/${path}`, {});
      await load();
      setPreviewKey((key) => key + 1);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That did not go through.",
      );
    }
  }

  async function upload(files: FileList | null) {
    if (!base || !files || files.length === 0) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const asset = await api.upload<Asset>(`${base}/assets`, file);
        // Put the path in the box: the person uploading a logo means to say
        // something about it, and this is the part they should not have to
        // type correctly.
        setMessage((text) => `${text}${text.trim() ? " " : ""}${asset.path} `);
      }
      await load();
      setError("");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That image could not be added.",
      );
    } finally {
      setUploading(false);
      if (picker.current) picker.current.value = "";
    }
  }

  async function removeAsset(asset: Asset) {
    if (!base) return;
    try {
      await api.del(`${base}/assets/${asset.id}`);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That image could not be removed.",
      );
    }
  }

  async function copyShareLink() {
    if (!project) return;
    await navigator.clipboard.writeText(project.share_url);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  if (!orgId || !project) return null;

  const latest = versions[0];
  const previewUrl = latest ? latest.url : "";

  return (
    <div className="flex h-[calc(100dvh-8rem)] min-h-[32rem] flex-col gap-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <Link
            to="/app/apps"
            aria-label="Back to apps"
            className="rounded-lg p-1.5 text-ink-400 transition hover:bg-ink-800/70 hover:text-ink-100"
          >
            <Icon path="M15 19l-7-7 7-7" />
          </Link>
          <h1 className="flex min-w-0 items-center gap-2.5 text-base font-medium text-ink-50">
            <span className="truncate">{project.name}</span>
            <Pill tone="warn">Beta</Pill>
          </h1>
          {project.published_version ? (
            <Pill tone="good">v{project.published_version} live</Pill>
          ) : null}
        </div>
        <div className="relative flex items-center gap-2">
          <Button
            variant="ghost"
            onClick={() => setHistory((open) => !open)}
            disabled={versions.length === 0}
          >
            {latest ? `v${latest.version}` : "No versions"}
            <span className="ml-1.5 text-ink-500">history</span>
          </Button>
          {project.published_version && (
            <Button variant="ghost" onClick={copyShareLink}>
              {copied ? "Link copied" : "Share"}
            </Button>
          )}
          {latest && (
            <Button
              disabled={latest.published}
              onClick={() => act(`versions/${latest.id}/deploy`)}
            >
              {latest.published ? "Deployed" : `Deploy v${latest.version}`}
            </Button>
          )}
          {history && (
            <VersionMenu
              versions={versions}
              published={project.published_version}
              busy={running}
              onClose={() => setHistory(false)}
              onDeploy={(version) => act(`versions/${version.id}/deploy`)}
              onRestore={(version) => act(`versions/${version.id}/restore`)}
              onUnpublish={() => act("unpublish")}
              codeUrl={(version) =>
                `${API_BASE}${base}/versions/${version.id}/source.zip`
              }
            />
          )}
        </div>
      </header>

      {error && <Alert tone="error">{error}</Alert>}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(20rem,26rem)_1fr]">
        <section className="flex min-h-0 min-w-0 flex-col rounded-2xl border border-ink-700/70 bg-ink-900/50">
          <Conversation
            turns={turns}
            running={running}
            activity={activity}
            onStarter={(text) => setMessage(text)}
          />
          <form
            onSubmit={send}
            className="flex-none border-t border-ink-700/70 p-3"
            aria-label="Describe a change"
          >
            {/* One box. The attachments, the text and the two controls belong
                to the same act, and three separate panels made a chat window
                look like a form. */}
            <div className="rounded-2xl border border-ink-600/70 bg-ink-900/60 transition focus-within:border-brand-400">
              {library && library.items.length > 0 && (
                <Uploads
                  library={library}
                  onPick={(asset) =>
                    setMessage(
                      (text) => `${text}${text.trim() ? " " : ""}${asset.path} `,
                    )
                  }
                  onRemove={removeAsset}
                  busy={running}
                />
              )}

              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    // Enter sends, which is what a chat box does everywhere
                    // else. Shift and Enter is the new line.
                    void send(event as unknown as FormEvent);
                  }
                }}
                rows={3}
                maxLength={8000}
                disabled={running}
                placeholder={
                  turns.length === 0
                    ? "A landing page for a bakery called Sunrise, with the menu and opening hours"
                    : "Make the header smaller and move the menu above it"
                }
                className="block w-full resize-none bg-transparent px-3.5 pt-3 pb-2 text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none disabled:opacity-60"
              />

              <div className="flex items-center justify-between gap-2 px-2 pb-2">
                <div className="flex items-center gap-1">
                  <input
                    ref={picker}
                    type="file"
                    accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
                    multiple
                    hidden
                    onChange={(event) => void upload(event.target.files)}
                  />
                  <button
                    type="button"
                    aria-label="Add an image"
                    title="Add an image for this app to use"
                    disabled={running || uploading}
                    onClick={() => picker.current?.click()}
                    className="rounded-lg p-2 text-ink-400 transition hover:bg-ink-800/70 hover:text-ink-100 disabled:opacity-40"
                  >
                    {uploading ? (
                      <Spinner className="h-4 w-4" />
                    ) : (
                      <Icon path="M21 15l-5-5L9 17M8.5 9.5a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM4 5h16v14H4z" />
                    )}
                  </button>
                  <p className="text-xs text-ink-500">
                    {running
                      ? "Working on your last message"
                      : "Enter to send, Shift and Enter for a new line"}
                  </p>
                </div>
                <button
                  type="submit"
                  aria-label="Send"
                  disabled={running || sending || !message.trim()}
                  className="rounded-xl bg-brand-500 p-2 text-ink-950 transition hover:brightness-110 disabled:bg-ink-700 disabled:text-ink-500"
                >
                  {running || sending ? (
                    <Spinner className="h-4 w-4" />
                  ) : (
                    <Icon path="M5 12h14M13 6l6 6-6 6" />
                  )}
                </button>
              </div>
            </div>
          </form>
        </section>

        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-ink-700/70 bg-ink-900/50">
          <div className="flex flex-none items-center justify-between gap-2 border-b border-ink-700/70 px-2 py-1.5">
            <div
              role="tablist"
              aria-label="Preview or code"
              className="flex items-center gap-0.5 rounded-xl bg-ink-900/70 p-0.5"
            >
              {(["preview", "code"] as const).map((which) => (
                <button
                  key={which}
                  role="tab"
                  aria-selected={tab === which}
                  onClick={() => setTab(which)}
                  className={cx(
                    "rounded-lg px-3 py-1.5 text-sm capitalize transition",
                    tab === which
                      ? "bg-ink-800 text-ink-100"
                      : "text-ink-400 hover:text-ink-200",
                  )}
                >
                  {which}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-0.5">
              <button
                type="button"
                aria-label="Reload the preview"
                title="Reload"
                disabled={!previewUrl}
                onClick={() => setPreviewKey((key) => key + 1)}
                className="rounded-lg p-2 text-ink-400 transition hover:bg-ink-800/70 hover:text-ink-100 disabled:opacity-40"
              >
                <Icon path="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6" />
              </button>
              <a
                href={previewUrl || undefined}
                target="_blank"
                rel="noreferrer noopener"
                aria-label="Open the preview in a new tab"
                title="Open in a new tab"
                className={cx(
                  "rounded-lg p-2 text-ink-400 transition hover:bg-ink-800/70 hover:text-ink-100",
                  !previewUrl && "pointer-events-none opacity-40",
                )}
              >
                <Icon path="M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
              </a>
            </div>
          </div>

          <div className="min-h-0 flex-1 bg-ink-950/40">
            {tab === "code" ? (
              files ? (
                <CodeBrowser files={files} />
              ) : (
                <div className="grid h-full place-items-center">
                  <Spinner className="h-5 w-5" />
                </div>
              )
            ) : previewUrl ? (
              <iframe
                key={previewKey}
                src={previewUrl}
                title={`${project.name} preview`}
                className="h-full w-full bg-white"
                sandbox="allow-scripts allow-forms allow-popups allow-modals"
              />
            ) : (
              <div className="grid h-full place-items-center p-8 text-center">
                <p className="max-w-sm text-sm text-ink-400">
                  Nothing built yet. Describe the page you want and it appears
                  here.
                </p>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * What to type when you have never typed one of these before.
 *
 * A blank box is the hardest screen in the product for somebody who is not a
 * developer. These are whole first messages, not categories: click one and it
 * is in the box, ready to send or edit, and the page it produces is a real
 * starting point rather than a placeholder.
 */
const STARTERS: { label: string; text: string }[] = [
  {
    label: "Landing page",
    text: "A landing page for a small business. Hero with a headline and a call to action button, three feature cards with icons, a testimonials section, pricing with three tiers, and a footer with contact details. Modern, generous spacing, subtle animations on scroll.",
  },
  {
    label: "Portfolio",
    text: "A personal portfolio for a designer. Name and one line intro at the top, a grid of six project cards that open a detail panel with a description, an about section, and a contact form. Clean and minimal with smooth hover effects.",
  },
  {
    label: "Restaurant menu",
    text: "A menu page for a restaurant. Sections for starters, mains, desserts and drinks with prices, a filter for vegetarian dishes, opening hours, address, and a reserve a table form. Warm and appetising.",
  },
  {
    label: "Dashboard",
    text: "An analytics dashboard. A sidebar, a header with a date range picker, four stat tiles with sparklines, a bar chart of weekly revenue built from divs, a table of recent orders with sorting, and a dark mode toggle. Use realistic sample data.",
  },
  {
    label: "Event page",
    text: "An event page for a one day conference. Countdown to the date, the schedule as a timeline, speaker cards with a short bio, ticket options, venue with directions, and a register form. Bold and energetic.",
  },
];

function Conversation({
  turns,
  running,
  activity,
  onStarter,
}: {
  turns: Turn[];
  running: boolean;
  /** What the agent is doing this second, or nothing yet. */
  activity: string;
  onStarter: (text: string) => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, running]);

  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
      {turns.length === 0 && (
        <div className="space-y-3">
          <p className="text-sm text-ink-400">
            Say what you want built. Plain words: what the page is for, what it
            should say, and who it is for. Or start from one of these and change
            what you like.
          </p>
          <div className="flex flex-wrap gap-2">
            {STARTERS.map((starter) => (
              <button
                key={starter.label}
                type="button"
                onClick={() => onStarter(starter.text)}
                className="rounded-full border border-ink-600/70 bg-ink-800/50 px-3 py-1.5 text-xs text-ink-200 transition hover:border-brand-400 hover:text-ink-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
              >
                {starter.label}
              </button>
            ))}
          </div>
        </div>
      )}
      {turns.map((turn) => (
        <motion.div
          key={turn.id}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className="space-y-2"
        >
          <p className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-brand-500/15 px-3.5 py-2.5 text-sm wrap-anywhere text-ink-100">
            {turn.prompt}
          </p>
          {turn.status === "built" && turn.reply && (
            <div className="mr-auto max-w-[85%] space-y-2 rounded-2xl rounded-bl-sm bg-ink-800/70 px-3.5 py-2.5 text-sm text-ink-200">
              {/* The agent writes markdown whether or not it was asked to, so
                  this is the chat window's own renderer rather than raw text
                  with the asterisks showing. */}
              <Formatted text={turn.reply} />
              {turn.version && (
                <p className="text-xs text-ink-500">v{turn.version}</p>
              )}
            </div>
          )}
          {turn.status === "failed" && (
            <p className="mr-auto max-w-[85%] rounded-2xl rounded-bl-sm border border-[color-mix(in_oklab,var(--status-bad)_35%,transparent)] bg-[color-mix(in_oklab,var(--status-bad)_10%,transparent)] px-3.5 py-2.5 text-sm text-ink-200">
              {turn.error || "That did not work."}
            </p>
          )}
          {(turn.status === "queued" || turn.status === "running") && (
            <Working since={turn.created_at} activity={activity} />
          )}
        </motion.div>
      ))}
      <div ref={end} />
    </div>
  );
}

/**
 * The images this app has, and how much room is left.
 *
 * They are shown because an image nobody can see is an image nobody uses:
 * clicking one puts its path in the message box, which is the whole of "use
 * this picture in the header". The two numbers are here rather than on the
 * billing page because this is where somebody finds out they have run out.
 */
function Uploads({
  library,
  onPick,
  onRemove,
  busy,
}: {
  library: Library;
  onPick: (asset: Asset) => void;
  onRemove: (asset: Asset) => void;
  busy: boolean;
}) {
  const left = Math.max(0, library.limit_bytes - library.used_bytes);
  return (
    <div className="space-y-1.5 border-b border-ink-700/60 px-2.5 pt-2.5 pb-2">
      <div className="flex flex-wrap gap-2">
        {library.items.map((asset) => (
          <div
            key={asset.id}
            className="group relative overflow-hidden rounded-lg border border-ink-600/70 bg-ink-800/50"
          >
            <button
              type="button"
              onClick={() => onPick(asset)}
              title={`${asset.filename}, ${size(asset.size_bytes)}. Click to use it in your message.`}
              className="block focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            >
              <img
                src={`${API_BASE}${asset.url}`}
                alt={asset.filename}
                className="h-14 w-14 object-cover"
              />
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onRemove(asset)}
              aria-label={`Remove ${asset.filename}`}
              className="absolute right-0.5 top-0.5 rounded-md bg-ink-900/80 px-1.5 text-xs text-ink-300 opacity-0 transition group-hover:opacity-100 focus-visible:opacity-100 hover:text-ink-50 disabled:opacity-0"
            >
              x
            </button>
          </div>
        ))}
      </div>
      <p className="text-xs text-ink-500">
        {size(library.used_bytes)} of images in this app, {size(left)} left.
        Click one to use it.
      </p>
    </div>
  );
}

/**
 * The wait, with a clock on it and what the agent is doing.
 *
 * A coding agent takes a minute or two, which is long enough that silence
 * reads as failure. Both halves here are facts rather than reassurance: the
 * line comes from the run's own events, so it says "Editing Menu.tsx" because
 * the agent edited Menu.tsx, and the elapsed time says the thing is still
 * going without promising when it will stop.
 */
function Working({ since, activity }: { since: string; activity: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const seconds = Math.max(
    0,
    Math.round((now - new Date(since).getTime()) / 1000),
  );

  return (
    <div className="mr-auto flex max-w-[85%] items-center gap-2.5 rounded-2xl rounded-bl-sm bg-ink-800/70 px-3.5 py-2.5 text-sm text-ink-300">
      <Spinner className="h-4 w-4" />
      <span className="min-w-0 truncate">{activity || "Starting"}</span>
      <span className="text-xs text-ink-500">
        {seconds < 60
          ? `${seconds}s`
          : `${Math.floor(seconds / 60)}m ${seconds % 60}s`}
      </span>
    </div>
  );
}

/** One line drawing, stroked in the current colour. */
function Icon({ path }: { path: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={path} />
    </svg>
  );
}

/**
 * Every build, newest first, in a menu rather than a rail.
 *
 * The rail was always visible and cost the preview sixty pixels of height on
 * every screen, to show something people touch about once a session: going
 * back to a version, or taking the code away. The preview is the point of
 * this page, so the list moved behind the version number.
 */
function VersionMenu({
  versions,
  published,
  busy,
  onClose,
  onDeploy,
  onRestore,
  onUnpublish,
  codeUrl,
}: {
  versions: Version[];
  published: number | null;
  busy: boolean;
  onClose: () => void;
  onDeploy: (version: Version) => void;
  onRestore: (version: Version) => void;
  onUnpublish: () => void;
  codeUrl: (version: Version) => string;
}) {
  const shown = useMemo(() => versions.slice(0, 20), [versions]);

  return (
    <>
      {/* A click anywhere else closes it. Cheaper than a listener on the
          document, and it cannot leak past unmount. */}
      <button
        type="button"
        aria-label="Close the version list"
        onClick={onClose}
        className="fixed inset-0 z-30 cursor-default"
      />
      <div className="absolute top-full right-0 z-40 mt-2 w-[22rem] overflow-hidden rounded-2xl border border-ink-700/70 bg-ink-900 shadow-2xl">
        <div className="flex items-center justify-between gap-2 border-b border-ink-700/70 px-3.5 py-2.5">
          <p className="text-sm font-medium text-ink-100">Versions</p>
          {published !== null && (
            <button
              type="button"
              onClick={() => {
                onUnpublish();
                onClose();
              }}
              className="text-xs text-ink-400 transition hover:text-ink-100"
            >
              Unpublish v{published}
            </button>
          )}
        </div>

        <ul className="max-h-[22rem] overflow-y-auto p-1.5">
          {shown.map((version) => (
            <li
              key={version.id}
              className="flex items-center gap-2 rounded-xl px-2 py-1.5 hover:bg-ink-800/60"
            >
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-2 text-sm text-ink-100">
                  v{version.version}
                  {version.published && <Pill tone="good">live</Pill>}
                </p>
                <p className="text-xs text-ink-500">
                  <RelativeTime value={version.created_at} />
                </p>
              </div>
              <a
                href={version.url}
                target="_blank"
                rel="noreferrer noopener"
                title="Open this version"
                className="rounded-lg p-1.5 text-ink-400 transition hover:bg-ink-700/60 hover:text-ink-100"
              >
                <Icon path="M14 4h6v6M20 4l-8.5 8.5M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
              </a>
              <a
                href={codeUrl(version)}
                title="Download this version as a project"
                className="rounded-lg p-1.5 text-ink-400 transition hover:bg-ink-700/60 hover:text-ink-100"
              >
                <Icon path="M12 4v12M7 12l5 5 5-5M5 20h14" />
              </a>
              <button
                type="button"
                disabled={busy}
                title="Start the next message from this version"
                onClick={() => {
                  onRestore(version);
                  onClose();
                }}
                className="rounded-lg p-1.5 text-ink-400 transition hover:bg-ink-700/60 hover:text-ink-100 disabled:opacity-40"
              >
                <Icon path="M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3" />
              </button>
              {!version.published && (
                <button
                  type="button"
                  onClick={() => {
                    onDeploy(version);
                    onClose();
                  }}
                  className="rounded-lg px-2 py-1 text-xs text-ink-300 transition hover:bg-ink-700/60 hover:text-ink-100"
                >
                  Deploy
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
