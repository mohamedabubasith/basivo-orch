import {
  motion,
  useReducedMotion,
  useScroll,
  useSpring,
  useTransform,
  type MotionValue,
} from "motion/react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { consoleOrigin } from "../../lib/consoleOrigin";

import { Backdrop } from "../Backdrop";
import { ThemeToggle } from "../ThemeToggle";
import { Badge, Button, Logo } from "../ui";
import { FlowAnimation } from "./FlowAnimation";
import { LogStream } from "./LogStream";

/**
 * Which optional media the build actually shipped.
 *
 * The hero video is optional: a checkout without it must still render a
 * finished page, and probing at run time
 * would flash an empty frame before the error handler fired. `import.meta.glob`
 * answers at build time instead, and the loader functions are never called, so
 * nothing extra is bundled.
 */
const PUBLIC_MEDIA = new Set(
  Object.keys(import.meta.glob("/public/*.{mp4,png,jpg,jpeg,webp}")).map(
    (path) => path.replace("/public", ""),
  ),
);

/** The path, if that file was in `public/` at build time. */
function asset(path: string): string | null {
  return PUBLIC_MEDIA.has(path) ? path : null;
}

/** Fade-and-rise on scroll, once, honouring the reduced-motion setting. */
function Reveal({
  children,
  delay = 0,
  className,
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const reduceMotion = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={reduceMotion ? false : { opacity: 0, y: 26, scale: 0.985 }}
      whileInView={{ opacity: 1, y: 0, scale: 1 }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{ duration: 0.6, delay, ease: [0.21, 0.5, 0.35, 1] }}
    >
      {children}
    </motion.div>
  );
}

/** A progress bar tied to page scroll. Cheap orientation on a long page. */
function ScrollProgress() {
  const { scrollYProgress } = useScroll();
  const scaleX = useSpring(scrollYProgress, {
    stiffness: 120,
    damping: 30,
    mass: 0.2,
  });
  return (
    <motion.div
      aria-hidden="true"
      style={{ scaleX }}
      className="fixed inset-x-0 top-0 z-[60] h-0.5 origin-left bg-gradient-to-r from-brand-500 to-accent-500"
    />
  );
}

/** A section heading block, used by every band below the hero. */
function Heading({
  eyebrow,
  title,
  lede,
}: {
  eyebrow: string;
  title: string;
  lede?: string;
}) {
  return (
    <Reveal className="mx-auto max-w-2xl text-center">
      <Badge className="mb-5">{eyebrow}</Badge>
      <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
        {title}
      </h2>
      {lede && (
        <p className="mt-4 text-lg leading-relaxed text-pretty text-ink-300">
          {lede}
        </p>
      )}
    </Reveal>
  );
}

/* ----------------------------------------------------------------- nav --- */

/**
 * A link into the application.
 *
 * Where the landing page has its own hostname this must be a real navigation
 * rather than a client-side route: a session cookie belongs to one origin, so
 * a visitor who signed up "here" would find the console asking them to sign in
 * again, with nothing on screen explaining why.
 */
function AppLink({
  to,
  children,
  className,
}: {
  to: string;
  children: ReactNode;
  className?: string;
}) {
  const origin = consoleOrigin();
  if (origin) {
    return (
      <a href={origin + to} className={className}>
        {children}
      </a>
    );
  }
  return (
    <Link to={to} className={className}>
      {children}
    </Link>
  );
}

export function Nav() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <>
      <ScrollProgress />
      <header
        className={`fixed inset-x-0 top-0 z-50 transition-all duration-300 ${
          scrolled
            ? "border-b border-ink-700/60 bg-ink-950/80 backdrop-blur-xl"
            : "border-b border-transparent"
        }`}
      >
        <nav className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-3 px-5">
          <Link to="/" className="rounded-lg" aria-label="Basivo home">
            <Logo />
          </Link>

          <div className="hidden items-center gap-7 md:flex">
            {[
              ["How it works", "#how"],
              ["What it does", "#does"],
              ["App builder", "#apps"],
              ["Compared", "#compare"],
              ["Pricing", "/pricing"],
            ].map(([label, href]) => (
              <a
                key={href}
                href={href}
                className="text-sm text-ink-300 transition-colors hover:text-ink-100"
              >
                {label}
              </a>
            ))}
          </div>

          <div className="flex flex-none items-center gap-2">
            {/* The console has had this since the first week and the page
                people see first did not, which made the product look like it
                only came in dark. */}
            <ThemeToggle compact />
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
              aria-label="Basivo on GitHub"
              title="Source on GitHub"
              className="rounded-lg p-2 text-ink-300 transition-colors hover:text-ink-100"
            >
              <GitHubMark />
            </a>
            <AppLink to="/login">
              <Button variant="ghost">Sign in</Button>
            </AppLink>
            <AppLink to="/register" className="hidden sm:inline-flex">
              <Button>Start free</Button>
            </AppLink>
          </div>
        </nav>
      </header>
    </>
  );
}

/** Where the source is. */
const REPO_URL = "https://github.com/mohamedabubasith/basivo-orch";

/** GitHub's mark, drawn in the current text colour so it works in both themes. */
function GitHubMark() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="18"
      height="18"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.4 7.4 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8" />
    </svg>
  );
}

/* ---------------------------------------------------------------- hero --- */

/**
 * What sits under the headline: the recorded product video when the build
 * shipped one, and the scripted run stream otherwise. Both are the same shape
 * on the page, so the section is finished either way.
 */
function HeroStage() {
  const video = asset("/hero.mp4");
  const poster = asset("/hero-poster.jpg") ?? asset("/hero-poster.png");

  if (!video) return <LogStream />;

  return (
    <video
      className="w-full rounded-2xl border border-[var(--edge-strong)] shadow-[0_24px_64px_-32px_rgba(0,0,0,0.6)]"
      src={video}
      poster={poster ?? undefined}
      autoPlay
      muted
      loop
      playsInline
      aria-label="What Basivo does: the ways a flow starts, the canvas, the agent, the videos it renders, and where the result is posted"
    />
  );
}

export function Hero() {
  const reduceMotion = useReducedMotion();
  const { scrollY } = useScroll();
  // The panel drifts slower than the page. Subtle (60px over a full screen),
  // because parallax that announces itself is worse than none.
  const panelY: MotionValue<number> = useTransform(
    scrollY,
    [0, 600],
    [0, reduceMotion ? 0 : 60],
  );
  const panelOpacity = useTransform(
    scrollY,
    [0, 500],
    [1, reduceMotion ? 1 : 0.72],
  );

  return (
    <section className="relative overflow-hidden pt-28 pb-20 sm:pt-32">
      <Backdrop />

      <div className="relative mx-auto max-w-6xl px-5">
        <motion.div
          className="mx-auto max-w-3xl text-center"
          initial={reduceMotion ? false : { opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.21, 0.5, 0.35, 1] }}
        >
          <Badge className="mb-6">
            <span className="h-1.5 w-1.5 rounded-full bg-ok-500" />
            Beta: building in the open
          </Badge>

          <h1 className="text-[2.4rem] leading-[1.08] font-semibold tracking-tight text-balance text-ink-100 sm:text-6xl">
            A ticket goes in,{" "}
            <motion.span
              className="text-gradient inline-block"
              initial={reduceMotion ? false : { opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.5,
                delay: 0.2,
                ease: [0.21, 0.5, 0.35, 1],
              }}
            >
              a pull request comes out.
            </motion.span>
          </h1>

          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-pretty text-ink-300">
            Basivo runs agent pipelines: something fires the trigger, a flow of
            nodes runs, and a pull request, a video or a post lands at the end.
          </p>

          <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <AppLink to="/register" className="w-full sm:w-auto">
              <Button size="lg" full className="sm:w-auto">
                Start free
              </Button>
            </AppLink>
            <a href="#how" className="w-full sm:w-auto">
              <Button size="lg" variant="secondary" full className="sm:w-auto">
                See how it works
              </Button>
            </a>
          </div>
        </motion.div>

        <motion.div
          className="relative mx-auto mt-14 max-w-4xl sm:mt-16"
          style={{ y: panelY, opacity: panelOpacity }}
          initial={reduceMotion ? false : { opacity: 0, y: 32, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{
            duration: 0.7,
            delay: 0.15,
            ease: [0.21, 0.5, 0.35, 1],
          }}
        >
          <div
            aria-hidden="true"
            className="absolute -inset-x-8 -top-6 bottom-0 rounded-[2rem] bg-gradient-to-b from-brand-500/12 to-transparent blur-2xl"
          />
          <div className="relative">
            <HeroStage />
          </div>
        </motion.div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- how --- */

const STEPS = [
  {
    n: "01",
    title: "Choose what starts it",
    body: "A GitHub issue, a Jira ticket, your own webhook, a schedule, or a message someone sends a Telegram bot. That is the trigger, and it is the only thing you have to wire up outside Basivo.",
  },
  {
    n: "02",
    title: "Draw the flow",
    body: "Drag nodes onto the canvas and join them up. An agent with tools, a condition, a bit of Python, an HTTP call, a render, a post. Publishing gives the flow a stable address.",
  },
  {
    n: "03",
    title: "Watch it run",
    body: "Every node reports its status, how long it took, the tokens it burned and what those cost. One real run against a real repository finished in 36 seconds for nine cents, and that number is printed on the run page.",
  },
] as const;

export function HowItWorks() {
  return (
    <section id="how" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Heading
          eyebrow="How it works"
          title="Three steps, and no config files"
          lede="You are drawing a pipeline, not writing YAML about one."
        />

        <Reveal className="mt-14">
          <FlowAnimation />
        </Reveal>

        <div className="mt-12 grid gap-8 md:grid-cols-3 md:gap-6">
          {STEPS.map((step, i) => (
            <Reveal key={step.n} delay={i * 0.1}>
              <div className="relative flex h-full flex-col">
                <span className="font-mono text-sm text-brand-400/70">
                  {step.n}
                </span>
                <h3 className="mt-3 text-lg font-semibold text-ink-100">
                  {step.title}
                </h3>
                <p className="mt-2 text-[0.95rem] leading-relaxed text-ink-400">
                  {step.body}
                </p>
                {i < STEPS.length - 1 && (
                  <span
                    aria-hidden="true"
                    className="absolute top-2 -right-3 hidden h-px w-6 bg-gradient-to-r from-ink-600 to-transparent md:block"
                  />
                )}
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------ features --- */

const GROUPS = [
  {
    title: "Agents, with the parts agents need",
    body: "An agent node that holds real tools. Skills it looks up when the job calls for one, rather than a prompt carrying every procedure it might ever need. Sub-agents, MCP servers, and a handover when the conversation belongs to somebody else.",
    items: ["Agent with tools", "Skills", "Sub-agents", "MCP", "Handover"],
    icon: "M8.5 8.5h7v7h-7zM12 3.5V8M12 16v4.5M3.5 12H8M16 12h4.5M8.5 5v3.5M15.5 5v3.5M8.5 15.5V19M15.5 15.5V19",
  },
  {
    title: "The plumbing in between",
    body: "Plain text generation for the jobs that need no tools. Code, HTTP requests and conditionals for everything a flow has to do between the clever bits. These are the unglamorous nodes, and no real pipeline works without them.",
    items: ["Text", "Code", "HTTP", "Conditionals"],
    icon: "M9.5 6 5 12l4.5 6M14.5 6 19 12l-4.5 6",
  },
  {
    title: "Things that come out the other end",
    body: "Rendered images with real fonts. Video through Remotion, where a composition is a React component. Spoken audio with word timings, and a montage built from photographs you supply. A run can finish with a file rather than a paragraph.",
    items: ["Images", "Video", "Speech", "Montage"],
    icon: "M3.5 5.5h13v13h-13zM16.5 10l4-2.5v9l-4-2.5M7 9.5v5l4-2.5z",
  },
  {
    title: "Somewhere for it to land",
    body: "Open a pull request on the repository the ticket came from. Post to Telegram, Discord, Slack, Mastodon or Bluesky, each with a credential you can make in about two minutes. Nothing sits behind a third-party posting service charging per message.",
    items: [
      "Pull requests",
      "Telegram",
      "Discord",
      "Slack",
      "Mastodon",
      "Bluesky",
    ],
    icon: "M20.5 3.8 3.9 10.2c-.9.3-.9 1.6 0 1.9l6.3 2.1 2.1 6.3c.3.9 1.6.9 1.9 0zM20.5 3.8 10.2 14.2",
  },
  {
    title: "An application, from a sentence",
    body: "Describe a page and watch it appear beside what you typed. Change your mind and it changes. Every build is a version you can go back to, deploy at an address you can send to anyone, or download as a project that runs anywhere Node does. Free plan included, no key of your own needed.",
    items: ["Live preview", "Versions", "Deploy", "Your images", "The code"],
    icon: "M3.5 5.5h17v13h-17zM3.5 9h17M6.5 7.2h.01M9 7.2h.01",
  },
] as const;

export function Features() {
  return (
    <section id="does" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Heading
          eyebrow="What it does"
          title="Four groups of nodes, and what each is for"
          lede="Naming all of them at once would just be a menu."
        />

        <div className="mt-14 grid gap-4 md:grid-cols-2">
          {GROUPS.map((group, i) => (
            <Reveal key={group.title} delay={(i % 2) * 0.07}>
              <motion.div
                whileHover={{ y: -4 }}
                transition={{ type: "spring", stiffness: 300, damping: 22 }}
                className="group surface flex h-full flex-col rounded-2xl p-6 transition-colors duration-300 hover:border-ink-500"
              >
                <div className="mb-4 inline-flex h-10 w-10 flex-none items-center justify-center rounded-xl border border-ink-600/60 bg-ink-850 text-brand-300 transition-colors group-hover:border-brand-400/50 group-hover:text-brand-400">
                  <svg
                    viewBox="0 0 24 24"
                    className="h-5 w-5"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d={group.icon}
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-ink-100">
                  {group.title}
                </h3>
                <p className="mt-2 text-[0.95rem] leading-relaxed text-ink-400">
                  {group.body}
                </p>
                <ul className="mt-5 flex flex-wrap gap-1.5">
                  {group.items.map((item) => (
                    <li
                      key={item}
                      className="rounded-full border border-ink-700/70 bg-ink-900/50 px-2.5 py-1 text-xs text-ink-300"
                    >
                      {item}
                    </li>
                  ))}
                </ul>
              </motion.div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------- compare --- */

const COMPARISON = [
  {
    them: "Wiring a trigger anywhere else",
    theirs:
      "Copy a webhook URL into the repository settings, paste a secret, choose the events, then keep all three in step by hand.",
    ours: "Pick the repository and tick the events. Publishing the flow registers the hook and keeps the secret. Delete the flow and the hook goes with it.",
  },
  {
    them: "n8n, Zapier",
    theirs:
      "General automation with an AI node added to the palette. Video, if you need it, is a service you run beside them.",
    ours: "Built around the agent, and the render is a node on the same canvas. Skills, sub-agents, handover, MCP and the price of each model call are part of the design rather than a late addition.",
  },
  {
    them: "Flowise and other chat builders",
    theirs: "Aimed at a chat window, and that is the whole surface.",
    ours: "Chat is one trigger of six. Drag the Chat node on, publish, and you have a hosted window to open or embed. The same flow can also run on a schedule and leave a file behind while nobody is watching.",
  },
  {
    them: "Hosted agent products",
    theirs: "Model spend is folded into a subscription you cannot see inside.",
    ours: "You bring your own key, so the provider bills you at their price. Each run shows what it cost.",
  },
] as const;

export function Compare() {
  return (
    <section id="compare" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Heading
          eyebrow="Compared"
          title="Where this is different, and where it is not"
          lede="Four things people ask about, and an honest answer for each."
        />

        <Reveal className="mt-14">
          <div className="surface overflow-hidden rounded-2xl">
            <div className="hidden grid-cols-[11rem_1fr_1.25fr] gap-5 border-b border-[var(--edge-strong)] px-6 py-4 text-xs tracking-[0.14em] text-ink-500 uppercase md:grid">
              <span>Instead of</span>
              <span>What they are</span>
              <span>What Basivo does</span>
            </div>
            {COMPARISON.map((row) => (
              <div
                key={row.them}
                className="grid gap-3 border-b border-[var(--edge)] px-6 py-5 last:border-0 md:grid-cols-[11rem_1fr_1.25fr] md:gap-5"
              >
                <p className="text-[0.95rem] font-semibold text-ink-100">
                  {row.them}
                </p>
                <p className="text-[0.9rem] leading-relaxed text-ink-400">
                  <span className="mb-1 block text-xs tracking-[0.14em] text-ink-500 uppercase md:hidden">
                    What they are
                  </span>
                  {row.theirs}
                </p>
                <p className="text-[0.9rem] leading-relaxed text-ink-200">
                  <span className="mb-1 block text-xs tracking-[0.14em] text-ink-500 uppercase md:hidden">
                    What Basivo does
                  </span>
                  {row.ours}
                </p>
              </div>
            ))}
          </div>
        </Reveal>

        <Reveal delay={0.08} className="mx-auto mt-6 max-w-3xl">
          <p className="text-center text-[0.95rem] leading-relaxed text-pretty text-ink-400">
            <span className="font-medium text-ink-200">
              Where the others win:
            </span>{" "}
            n8n and Zapier connect to hundreds of applications. Basivo has a
            couple of dozen node types. If the job is moving rows between SaaS
            tools, use one of those instead and enjoy the afternoon off.
          </p>
        </Reveal>
      </div>
    </section>
  );
}

/* --------------------------------------------------------------- trust --- */

const TRUST = [
  {
    title: "Your keys stay yours",
    body: "Bring your own key for OpenAI, Anthropic, Gemini, Groq and the rest. The provider bills you directly at their price, so there is no markup on tokens. We never sit in the middle of that transaction.",
  },
  {
    title: "Run it on your own box",
    body: "One server, Docker, Postgres and Redis. Self-hosting is the supported path rather than a grudging concession, and an operator can switch billing off entirely.",
  },
  {
    title: "Every run is auditable",
    body: "Runs are records, not console output that scrolls away. Open one from last month and the per node status, duration, tokens and cost are all still there. When something fails you see which node failed and why.",
  },
  {
    title: "The cost is on the page",
    body: "Nine cents is not a figure from a pitch deck. It is what one real run cost against a real repository, and it sits on that run next to the 36 seconds it took.",
  },
] as const;

export function Trust() {
  return (
    <section id="trust" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Heading
          eyebrow="Trust"
          title="Nothing here needs taking on faith"
          lede="Every claim on this page is one you can check from inside the product on your first afternoon."
        />

        <div className="mt-14 grid gap-4 sm:grid-cols-2">
          {TRUST.map((item, i) => (
            <Reveal key={item.title} delay={(i % 2) * 0.07}>
              <div className="surface h-full rounded-2xl p-6 transition-colors duration-300 hover:border-ink-500">
                <h3 className="text-base font-semibold text-ink-100">
                  {item.title}
                </h3>
                <p className="mt-2.5 text-[0.95rem] leading-relaxed text-ink-400">
                  {item.body}
                </p>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ----------------------------------------------------------- app builder --- */

/**
 * What the App Builder feels like, in one card.
 *
 * A screenshot would be out of date by the next release and a video would cost
 * a megabyte before anybody scrolled this far. This is the real shape of the
 * screen instead, drawn in divs: what you typed on the left, what exists on
 * the right, and the versions underneath. It plays once when it comes into
 * view, and not at all for somebody who asked for less motion.
 */
const BUILD_STEPS = [
  { at: 0.0, label: "Reading the project" },
  { at: 1.1, label: "Editing App.tsx" },
  { at: 2.2, label: "Building the page" },
] as const;

export function AppBuilderSection() {
  const reduceMotion = useReducedMotion();
  const ease = [0.21, 0.5, 0.35, 1] as const;

  return (
    <section id="apps" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Heading
          eyebrow="App builder"
          title="Describe a page, and watch it appear"
          lede="The same run engine, pointed at a React project instead of a repository. On the free plan, with no key of your own."
        />

        <Reveal className="mt-14">
          <div className="surface overflow-hidden rounded-3xl p-3 sm:p-4">
            <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_1.25fr] md:gap-4">
              {/* what you typed */}
              <div className="flex min-h-[15rem] flex-col rounded-2xl border border-ink-700/60 bg-ink-900/60 p-4">
                <motion.p
                  className="ml-auto max-w-[90%] rounded-2xl rounded-br-sm bg-brand-500/15 px-3.5 py-2.5 text-sm text-ink-100"
                  initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.4 }}
                  transition={{ duration: 0.45, ease }}
                >
                  A page for a corner shop called Bright Grocers, with the
                  opening hours and a photograph at the top
                </motion.p>

                <div className="mt-3 space-y-2">
                  {BUILD_STEPS.map((step) => (
                    <motion.p
                      key={step.label}
                      className="flex items-center gap-2 text-sm text-ink-400"
                      initial={reduceMotion ? false : { opacity: 0, x: -6 }}
                      whileInView={{ opacity: 1, x: 0 }}
                      viewport={{ once: true, amount: 0.4 }}
                      transition={{ duration: 0.35, delay: 0.4 + step.at * 0.35, ease }}
                    >
                      <span className="h-1.5 w-1.5 flex-none rounded-full bg-brand-400" />
                      {step.label}
                    </motion.p>
                  ))}
                </div>

                <motion.p
                  className="mt-3 mr-auto max-w-[90%] rounded-2xl rounded-bl-sm bg-ink-800/70 px-3.5 py-2.5 text-sm text-ink-300"
                  initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.4 }}
                  transition={{ duration: 0.4, delay: 1.6, ease }}
                >
                  Your photograph is at the top, with the hours beside it.
                </motion.p>
              </div>

              {/* what exists */}
              <div className="overflow-hidden rounded-2xl border border-ink-700/60 bg-ink-950/60">
                <div className="flex items-center gap-2 border-b border-ink-700/60 px-4 py-2.5">
                  <span className="h-2 w-2 rounded-full bg-ink-600" />
                  <span className="h-2 w-2 rounded-full bg-ink-600" />
                  <span className="h-2 w-2 rounded-full bg-ink-600" />
                  <span className="ml-2 truncate font-mono text-xs text-ink-500">
                    bright-grocers-k3d9.basivo.app
                  </span>
                </div>

                <motion.div
                  className="space-y-3 p-4"
                  initial={reduceMotion ? false : { opacity: 0 }}
                  whileInView={{ opacity: 1 }}
                  viewport={{ once: true, amount: 0.4 }}
                  transition={{ duration: 0.5, delay: 1.5, ease }}
                >
                  <motion.div
                    className="h-24 rounded-xl bg-gradient-to-br from-brand-500/30 via-brand-400/15 to-transparent sm:h-28"
                    initial={reduceMotion ? false : { scale: 0.96, opacity: 0 }}
                    whileInView={{ scale: 1, opacity: 1 }}
                    viewport={{ once: true, amount: 0.4 }}
                    transition={{ duration: 0.5, delay: 1.6, ease }}
                  />
                  <div className="h-3 w-2/5 rounded-full bg-ink-700" />
                  <div className="h-2.5 w-4/5 rounded-full bg-ink-800" />
                  <div className="grid grid-cols-3 gap-2 pt-1">
                    {[0, 1, 2].map((card) => (
                      <motion.div
                        key={card}
                        className="h-12 rounded-lg border border-ink-700/60 bg-ink-900/70"
                        initial={reduceMotion ? false : { opacity: 0, y: 10 }}
                        whileInView={{ opacity: 1, y: 0 }}
                        viewport={{ once: true, amount: 0.4 }}
                        transition={{
                          duration: 0.4,
                          delay: 1.8 + card * 0.1,
                          ease,
                        }}
                      />
                    ))}
                  </div>
                </motion.div>
              </div>
            </div>

            {/* the versions, which is what makes changing your mind cheap */}
            <div className="mt-3 flex flex-wrap items-center gap-2 rounded-2xl border border-ink-700/60 bg-ink-900/40 p-3 sm:mt-4">
              {["v1", "v2", "v3"].map((version, i) => (
                <motion.span
                  key={version}
                  className={
                    i === 2
                      ? "rounded-lg border border-ok-500/40 bg-ok-500/10 px-2.5 py-1 text-xs text-ink-200"
                      : "rounded-lg border border-ink-700/70 px-2.5 py-1 text-xs text-ink-400"
                  }
                  initial={reduceMotion ? false : { opacity: 0, y: 6 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.4 }}
                  transition={{ duration: 0.3, delay: 2 + i * 0.12, ease }}
                >
                  {version}
                  {i === 2 && " deployed"}
                </motion.span>
              ))}
              <span className="ml-auto text-xs text-ink-500">
                Go back to any version, or download the whole project
              </span>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- cta --- */


export function CTA() {
  return (
    <section className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-4xl px-5">
        <Reveal>
          <div className="surface relative overflow-hidden rounded-3xl px-6 py-14 text-center sm:px-8">
            <div
              aria-hidden="true"
              className="pointer-events-none absolute -top-24 left-1/2 h-64 w-[560px] -translate-x-1/2 rounded-full bg-ink-100 opacity-[0.04] blur-[110px]"
            />
            <div className="relative">
              <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
                Point it at one open ticket
              </h2>
              <p className="mx-auto mt-4 max-w-lg text-lg text-pretty text-ink-300">
                The free plan asks for no card. Pick the smallest issue in your
                backlog and see what comes back.
              </p>
              <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
                <AppLink to="/register" className="w-full sm:w-auto">
                  <Button size="lg" full className="sm:w-auto">
                    Create your account
                  </Button>
                </AppLink>
                <AppLink to="/login" className="w-full sm:w-auto">
                  <Button
                    size="lg"
                    variant="secondary"
                    full
                    className="sm:w-auto"
                  >
                    Sign in
                  </Button>
                </AppLink>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

/* -------------------------------------------------------------- footer --- */

/** The sections of this page. In the footer as well as the bar, because the
 *  bar hides them under `md` and a phone would otherwise have no way there. */
const PRODUCT: [string, string][] = [
  ["How it works", "/#how"],
  ["What it does", "/#does"],
  ["App builder", "/#apps"],
  ["Compared", "/#compare"],
  ["Trust", "/#trust"],
];

/** The pages a customer, and a payment provider, expect to find in a footer. */
const LEGAL: [string, string][] = [
  ["Pricing", "/pricing"],
  ["Terms", "/terms"],
  ["Privacy", "/privacy"],
  ["Refunds", "/refunds"],
  ["Contact", "/contact"],
];

export function Footer() {
  return (
    <footer className="border-t border-ink-800/70 py-10">
      <div className="mx-auto flex max-w-6xl flex-col items-center gap-6 px-5 text-center">
        <nav className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2 md:hidden">
          {PRODUCT.map(([label, to]) => (
            <Link
              key={to}
              to={to}
              className="text-sm text-ink-400 transition-colors hover:text-ink-100"
            >
              {label}
            </Link>
          ))}
        </nav>
        <div className="flex w-full flex-col items-center justify-between gap-4 sm:flex-row sm:text-left">
          <Logo />
          <nav className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2">
            {LEGAL.map(([label, to]) => (
              <Link
                key={to}
                to={to}
                className="text-sm text-ink-400 transition-colors hover:text-ink-100"
              >
                {label}
              </Link>
            ))}
          </nav>
        </div>
        <p className="text-sm text-ink-500">
          © {new Date().getFullYear()} Basivo. Beta software. Expect sharp
          edges. Payments by Dodo Payments, our merchant of record.
        </p>
      </div>
    </footer>
  );
}
