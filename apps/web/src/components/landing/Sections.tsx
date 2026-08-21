import {
  motion,
  useReducedMotion,
  useScroll,
  useSpring,
  useTransform,
  type MotionValue,
} from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Backdrop } from "../Backdrop";
import { Badge, Button, Logo } from "../ui";
import { LogStream } from "./LogStream";

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

/** Counts up when it scrolls into view. */
function Counter({ to, suffix = "" }: { to: number; suffix?: string }) {
  const reduceMotion = useReducedMotion();
  const [value, setValue] = useState(reduceMotion ? to : 0);
  const ref = useRef<HTMLSpanElement>(null);
  const started = useRef(false);

  useEffect(() => {
    if (reduceMotion || !ref.current) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting || started.current) return;
        started.current = true;
        const start = performance.now();
        const step = (now: number) => {
          const t = Math.min(1, (now - start) / 900);
          // Ease-out: fast then settling, which reads as counting rather than
          // sliding.
          setValue(Math.round(to * (1 - Math.pow(1 - t, 3))));
          if (t < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      },
      { threshold: 0.4 },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [to, reduceMotion]);

  return (
    <span ref={ref}>
      {value.toLocaleString()}
      {suffix}
    </span>
  );
}

/* ----------------------------------------------------------------- nav --- */

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
        <nav className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5">
          <Link to="/" className="rounded-lg" aria-label="Basivo home">
            <Logo />
          </Link>

          <div className="hidden items-center gap-7 md:flex">
            {[
              ["Observability", "#observability"],
              ["Features", "#features"],
              ["How it works", "#how"],
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

          <div className="flex items-center gap-2">
            <Link to="/login">
              <Button variant="ghost">Sign in</Button>
            </Link>
            <Link to="/register">
              <Button>Start free</Button>
            </Link>
          </div>
        </nav>
      </header>
    </>
  );
}

/* --------------------------------------------------------------- stats --- */

export function Stats() {
  // Counted from the node registry and the provider table, not rounded up.
  // A landing page whose numbers do not survive `make dev` is a landing page
  // nobody on the team trusts.
  const items = [
    { to: 15, suffix: "", label: "node types, from triggers to video" },
    { to: 20, suffix: "", label: "model providers, your keys" },
    { to: 100, suffix: "%", label: "of steps logged with tokens and cost" },
  ];
  return (
    <section className="relative border-t border-ink-800/70 py-16">
      <div className="mx-auto grid max-w-5xl gap-8 px-5 sm:grid-cols-3">
        {items.map((item, i) => (
          <Reveal key={item.label} delay={i * 0.08} className="text-center">
            <p className="text-4xl font-semibold tracking-tight text-gradient">
              <Counter to={item.to} suffix={item.suffix} />
            </p>
            <p className="mt-2 text-sm text-ink-400">{item.label}</p>
          </Reveal>
        ))}
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- hero --- */

export function Hero() {
  const reduceMotion = useReducedMotion();
  const { scrollY } = useScroll();
  // The panel drifts slower than the page. Subtle — 60px over a full screen —
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
    <section className="relative overflow-hidden pt-32 pb-20">
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

          <h1 className="text-[2.6rem] leading-[1.08] font-semibold tracking-tight text-balance text-ink-100 sm:text-6xl">
            {"An issue at 3am is a".split(" ").map((word, i) => (
              <motion.span
                key={word + i}
                className="inline-block"
                initial={reduceMotion ? false : { opacity: 0, y: 18 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{
                  duration: 0.5,
                  delay: 0.05 * i,
                  ease: [0.21, 0.5, 0.35, 1],
                }}
              >
                {word}&nbsp;
              </motion.span>
            ))}
            <motion.span
              className="text-gradient inline-block"
              initial={reduceMotion ? false : { opacity: 0, y: 18 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.5,
                delay: 0.25,
                ease: [0.21, 0.5, 0.35, 1],
              }}
            >
              pull request by breakfast
            </motion.span>
          </h1>

          <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-pretty text-ink-300">
            Draw the pipeline once: a bug report arrives, an agent reads it
            (screenshot and all), finds the cause in your repository, and opens
            a pull request for a human to review. Then watch every step of it
            run: which tool was called, how many tokens, what it cost.
          </p>

          <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <Link to="/register" className="w-full sm:w-auto">
              <Button size="lg" full className="sm:w-auto">
                Start building free
              </Button>
            </Link>
            <a href="#observability" className="w-full sm:w-auto">
              <Button size="lg" variant="secondary" full className="sm:w-auto">
                See the run view
              </Button>
            </a>
          </div>

          <p className="mt-4 text-sm text-ink-500">Self-host or cloud.</p>
        </motion.div>

        <motion.div
          className="relative mx-auto mt-16 max-w-4xl"
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
            <LogStream />
          </div>
        </motion.div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------- observability --- */

const OBSERVABILITY = [
  {
    title: "Every run, kept",
    body: "Runs are records, not console output. Filter by pipeline, status, duration or trace id, and open one from three weeks ago with its logs intact.",
  },
  {
    title: "Step-level timing",
    body: "Duration and token cost per node, so you can see which step is slow and which one is expensive. Usually not the same step.",
  },
  {
    title: "Retries you can read",
    body: "Every attempt is logged with its backoff and its error. A step that succeeded on attempt three does not look like a step that succeeded.",
  },
  {
    title: "Structured, not stringly",
    body: "Levelled, timestamped, node-attributed lines. Query them instead of scrolling. The same data the run view renders is the data you can export.",
  },
];

export function Observability() {
  return (
    <section
      id="observability"
      className="relative border-t border-ink-800/70 py-24"
    >
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mx-auto max-w-2xl text-center">
          <Badge className="mb-5">Observability</Badge>
          <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
            Most tools show you a green tick
          </h2>
          <p className="mt-4 text-lg leading-relaxed text-pretty text-ink-300">
            That is fine until something breaks. Basivo treats the run log as
            the product: it is the first thing you see, not a tab you go looking
            for.
          </p>
        </Reveal>

        <div className="mt-14 grid gap-4 sm:grid-cols-2">
          {OBSERVABILITY.map((item, i) => (
            <Reveal key={item.title} delay={i * 0.07}>
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

/* ------------------------------------------------------------ features --- */

const FEATURES = [
  {
    title: "It reads the screenshot",
    body: "Bug reports are pictures more often than prose. The repair agent looks at the image attached to an issue, not just the words around it.",
    icon: "M4.5 5.5h15v13h-15zM4.5 14l4-4 3.5 3.5M13 12.5l2.5-2.5 4 4M9 9.2a1.2 1.2 0 1 1 0-.1",
  },
  {
    title: "Pull requests, never merges",
    body: "The fix arrives on a branch with the agent's own explanation of what was wrong. Protected paths like CI config are refused outright. You review; it never merges.",
    icon: "M7 6a2 2 0 1 1 0-.1M7 8v8M7 18a2 2 0 1 1 0-.1M17 12a2 2 0 1 1 0-.1M9 6.5c5 0 6 2 6 5.5",
  },
  {
    title: "Agents that hand over",
    body: "One agent asks another and keeps control, or transfers the conversation entirely. Every hand-off is on the log with the tokens it cost.",
    icon: "M4 8h11l-3-3M20 16H9l3 3",
  },
  {
    title: "Posters and video, rendered",
    body: "A model writes the layout; a browser renders it with real fonts. Your headline is your headline, not an image model's ninety-percent guess at typography.",
    icon: "M3.5 5.5h13v13h-13zM16.5 10l4-2.5v9l-4-2.5M7 9.5v5l4-2.5z",
  },
  {
    title: "Publish for nothing",
    body: "Telegram, Discord, Bluesky, Mastodon and Slack, each with a credential you make in two minutes. No third-party posting service, no per-post fee.",
    icon: "M20.5 3.8 3.9 10.2c-.9.3-.9 1.6 0 1.9l6.3 2.1 2.1 6.3c.3.9 1.6.9 1.9 0zM20.5 3.8 10.2 14.2",
  },
  {
    title: "Runs that survive a deploy",
    body: "Work is queued in Postgres and executed by workers. Restart the API mid-run and the run keeps going; kill a worker and another picks it up.",
    icon: "M12 3.5a8.5 8.5 0 1 1-8 5.7M12 3.5V9M4 9.2h5.5",
  },
];

export function Features() {
  return (
    <section
      id="features"
      className="relative border-t border-ink-800/70 py-24"
    >
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mx-auto max-w-2xl text-center">
          <Badge className="mb-5">Platform</Badge>
          <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
            Everything a pipeline needs to run in production
          </h2>
        </Reveal>

        <div className="mt-14 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((feature, i) => (
            <Reveal key={feature.title} delay={(i % 3) * 0.07}>
              <motion.div
                whileHover={{ y: -4 }}
                transition={{ type: "spring", stiffness: 300, damping: 22 }}
                className="group surface h-full rounded-2xl p-6 transition-colors duration-300 hover:border-ink-500 hover:bg-ink-800/50"
              >
                <div className="mb-4 inline-flex h-10 w-10 items-center justify-center rounded-xl border border-ink-600/60 bg-ink-850 text-brand-300 transition-colors group-hover:border-brand-400/50 group-hover:text-brand-400">
                  <svg
                    viewBox="0 0 24 24"
                    className="h-5 w-5"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d={feature.icon}
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-ink-100">
                  {feature.title}
                </h3>
                <p className="mt-2 text-[0.95rem] leading-relaxed text-ink-400">
                  {feature.body}
                </p>
              </motion.div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- how --- */

const STEPS = [
  {
    n: "01",
    title: "Draw it once",
    body: "Pick a trigger (a webhook, a schedule, a GitHub issue), then add the steps: an agent, a condition, a repair, a render, a post.",
  },
  {
    n: "02",
    title: "Point something at it",
    body: "Paste the URL into your repository's webhook settings, set a cron, or call it from your own backend. Publishing gives it a stable address.",
  },
  {
    n: "03",
    title: "Read what happened",
    body: "Every step keeps its input, output, duration, tokens and cost, and the files it made. Posters and video play in the run itself.",
  },
];

export function HowItWorks() {
  return (
    <section id="how" className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal className="mx-auto max-w-2xl text-center">
          <Badge className="mb-5">How it works</Badge>
          <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
            Three steps, no config files
          </h2>
        </Reveal>

        <div className="mt-14 grid gap-6 md:grid-cols-3">
          {STEPS.map((step, i) => (
            <Reveal key={step.n} delay={i * 0.1}>
              <div className="relative h-full">
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

/* ---------------------------------------------------------------- cta --- */

export function CTA() {
  return (
    <section className="relative border-t border-ink-800/70 py-24">
      <div className="mx-auto max-w-4xl px-5">
        <Reveal>
          <div className="surface relative overflow-hidden rounded-3xl px-8 py-14 text-center">
            <div
              aria-hidden="true"
              className="pointer-events-none absolute -top-24 left-1/2 h-64 w-[560px] -translate-x-1/2 rounded-full bg-ink-100 opacity-[0.04] blur-[110px]"
            />
            <div className="relative">
              <h2 className="text-3xl font-semibold tracking-tight text-balance text-ink-100 sm:text-4xl">
                Ship your first pipeline today
              </h2>
              <p className="mx-auto mt-4 max-w-lg text-lg text-pretty text-ink-300">
                Free while we are in beta. Your feedback shapes what we build
                next.
              </p>
              <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
                <Link to="/register" className="w-full sm:w-auto">
                  <Button size="lg" full className="sm:w-auto">
                    Create your account
                  </Button>
                </Link>
                <Link to="/login" className="w-full sm:w-auto">
                  <Button
                    size="lg"
                    variant="secondary"
                    full
                    className="sm:w-auto"
                  >
                    Sign in
                  </Button>
                </Link>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

/* -------------------------------------------------------------- footer --- */

export function Footer() {
  return (
    <footer className="border-t border-ink-800/70 py-10">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-5 sm:flex-row">
        <Logo />
        <p className="text-sm text-ink-500">
          © {new Date().getFullYear()} Basivo. Beta software. Expect sharp
          edges.
        </p>
      </div>
    </footer>
  );
}
