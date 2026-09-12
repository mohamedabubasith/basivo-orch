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
  const picker = useRef<HTMLInputElement>(null);

  const base = orgId ? `/api/v1/orgs/${orgId}/apps/${appId}` : "";
  const running = turns.some(
    (turn) => turn.status === "queued" || turn.status === "running",
  );
  const builtCount = versions.length;

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
        <div className="min-w-0">
          <Link
            to="/app/apps"
            className="text-sm text-ink-400 transition hover:text-ink-200"
          >
            Apps
          </Link>
          <h1 className="truncate text-xl font-medium text-ink-50">
            {project.name}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {project.published_version ? (
            <Pill tone="good">deployed v{project.published_version}</Pill>
          ) : (
            <Pill>not deployed</Pill>
          )}
          {project.published_version && (
            <>
              <Button variant="ghost" onClick={copyShareLink}>
                {copied ? "Link copied" : "Copy link"}
              </Button>
              <Button variant="ghost" onClick={() => act("unpublish")}>
                Unpublish
              </Button>
            </>
          )}
          {latest && (
            <Button
              disabled={latest.published}
              onClick={() => act(`versions/${latest.id}/deploy`)}
            >
              {latest.published ? "Deployed" : `Deploy v${latest.version}`}
            </Button>
          )}
        </div>
      </header>

      {error && <Alert tone="error">{error}</Alert>}

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(20rem,26rem)_1fr]">
        <section className="flex min-h-0 flex-col rounded-2xl border border-ink-700/70 bg-ink-900/50">
          <Conversation
            turns={turns}
            running={running}
            activity={activity}
            onStarter={(text) => setMessage(text)}
          />
          <form
            onSubmit={send}
            className="border-t border-ink-700/70 p-3"
            aria-label="Describe a change"
          >
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
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
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
              className="block w-full resize-none rounded-xl border border-ink-600/70 bg-ink-900/60 px-3.5 py-3 text-sm text-ink-100 placeholder:text-ink-500 focus:border-brand-400 focus:outline-none disabled:opacity-60"
            />
            <div className="mt-2 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <input
                  ref={picker}
                  type="file"
                  accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
                  multiple
                  hidden
                  onChange={(event) => void upload(event.target.files)}
                />
                <Button
                  type="button"
                  variant="ghost"
                  disabled={running || uploading}
                  onClick={() => picker.current?.click()}
                >
                  {uploading ? "Adding" : "Add image"}
                </Button>
                <p className="text-xs text-ink-500">
                  {running ? "Working on your last message" : "Enter to add a line"}
                </p>
              </div>
              <Button
                type="submit"
                disabled={running || sending || !message.trim()}
              >
                {running ? "Working" : "Send"}
              </Button>
            </div>
          </form>
        </section>

        <section className="flex min-h-0 flex-col gap-3">
          <div className="relative min-h-0 flex-1 overflow-hidden rounded-2xl border border-ink-700/70 bg-white">
            {previewUrl ? (
              <iframe
                key={previewKey}
                src={previewUrl}
                title={`${project.name} preview`}
                className="h-full w-full"
                sandbox="allow-scripts allow-forms allow-popups allow-modals"
              />
            ) : (
              <div className="grid h-full place-items-center bg-ink-900/60 p-8 text-center">
                <p className="max-w-sm text-sm text-ink-400">
                  Nothing built yet. Describe the page you want and it appears
                  here.
                </p>
              </div>
            )}
          </div>
          <VersionRail
            versions={versions}
            onDeploy={(version) => act(`versions/${version.id}/deploy`)}
            onRestore={(version) => act(`versions/${version.id}/restore`)}
            busy={running}
            codeUrl={(version) =>
              `${API_BASE}${base}/versions/${version.id}/source.zip`
            }
          />
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
          <p className="ml-auto max-w-[85%] rounded-2xl rounded-br-sm bg-brand-500/15 px-3.5 py-2.5 text-sm text-ink-100">
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
    <div className="mb-2 space-y-1.5">
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

/**
 * Every build, newest first, with the two things you can do to one.
 *
 * Deploy points the share link at it. Go back makes it what the next message
 * starts from, which is undo, and leaves the newer versions alone so changing
 * your mind twice costs nothing.
 */
function VersionRail({
  versions,
  onDeploy,
  onRestore,
  busy,
  codeUrl,
}: {
  versions: Version[];
  onDeploy: (version: Version) => void;
  onRestore: (version: Version) => void;
  busy: boolean;
  /** Where a version's code downloads from, given its id. */
  codeUrl: (version: Version) => string;
}) {
  const shown = useMemo(() => versions.slice(0, 12), [versions]);
  if (shown.length === 0) return null;

  return (
    <div className="flex gap-2 overflow-x-auto rounded-2xl border border-ink-700/70 bg-ink-900/50 p-3">
      {shown.map((version) => (
        <div
          key={version.id}
          className={cx(
            "flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2",
            version.published
              ? "border-[color-mix(in_oklab,var(--status-good)_45%,transparent)] bg-[color-mix(in_oklab,var(--status-good)_10%,transparent)]"
              : "border-ink-700/70 bg-ink-800/40",
          )}
        >
          <div className="text-sm text-ink-200">
            v{version.version}
            <span className="ml-2 text-xs text-ink-500">
              <RelativeTime value={version.created_at} />
            </span>
          </div>
          <a
            href={version.url}
            target="_blank"
            rel="noreferrer noopener"
            className="rounded-lg px-2 py-1 text-xs text-ink-300 transition hover:bg-ink-700/60 hover:text-ink-100"
          >
            Open
          </a>
          {!version.published && (
            <button
              type="button"
              onClick={() => onDeploy(version)}
              className="rounded-lg px-2 py-1 text-xs text-ink-300 transition hover:bg-ink-700/60 hover:text-ink-100"
            >
              Deploy
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() => onRestore(version)}
            className="rounded-lg px-2 py-1 text-xs text-ink-300 transition hover:bg-ink-700/60 hover:text-ink-100 disabled:opacity-50"
          >
            Go back to this
          </button>
          <a
            href={codeUrl(version)}
            className="rounded-lg px-2 py-1 text-xs text-ink-300 transition hover:bg-ink-700/60 hover:text-ink-100"
          >
            Download code
          </a>
        </div>
      ))}
    </div>
  );
}
