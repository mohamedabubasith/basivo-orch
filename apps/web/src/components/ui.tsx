import { motion } from "motion/react";
import {
  forwardRef,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";

import { cx } from "../lib/cx";

/* -------------------------------------------------------------- button --- */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost";
  size?: "md" | "lg";
  loading?: boolean;
  full?: boolean;
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  full = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  const base =
    "group relative inline-flex items-center justify-center gap-2 rounded-lg " +
    "font-medium transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-55 " +
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";

  const variants = {
    // A crisp solid with a hairline top-light, not a coloured glow — drop
    // shadows in the button's own hue are the fastest way to look like 2021.
    primary:
      "bg-brand-500 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.18),0_1px_2px_rgba(0,0,0,0.25)] " +
      "hover:bg-brand-400 active:bg-brand-500",
    secondary:
      "border border-[var(--edge-strong)] bg-ink-850 text-ink-100 hover:bg-ink-800",
    ghost: "text-ink-300 hover:text-ink-100 hover:bg-ink-800/60",
  } as const;

  const sizes = { md: "h-10 px-4 text-sm", lg: "h-12 px-6 text-[0.95rem]" } as const;

  return (
    <button
      className={cx(base, variants[variant], sizes[size], full && "w-full", className)}
      disabled={disabled || loading}
      // Tell assistive tech the control is busy rather than silently inert.
      aria-busy={loading || undefined}
      {...rest}
    >
      {/* One spinner, laid over the hidden label so the button does not change
          width mid-submit. An earlier version rendered a second inline spinner
          as well, which showed two at once. */}
      <span className={cx("inline-flex items-center gap-2", loading && "invisible")}>
        {children}
      </span>
      {loading && (
        <span className="absolute inset-0 grid place-items-center">
          <Spinner />
        </span>
      )}

    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cx("h-4 w-4 animate-spin", className)}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
      <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v3a5 5 0 0 0-5 5H4z" />
    </svg>
  );
}

/** Full-page loading state, for when there is nothing yet to show. */
export function PageLoader({ label = "Loading" }: { label?: string }) {
  return (
    <div className="grid min-h-dvh place-items-center" role="status" aria-live="polite">
      <div className="flex flex-col items-center gap-4">
        <span className="relative flex h-10 w-10">
          <span className="absolute inset-0 animate-ping rounded-full bg-brand-500/30" />
          <span className="relative m-auto h-6 w-6">
            <Spinner className="h-6 w-6 text-brand-400" />
          </span>
        </span>
        <span className="text-sm text-ink-400">{label}…</span>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- field --- */

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  /** Renders a show/hide toggle. Only meaningful for type="password". */
  revealable?: boolean;
};

export const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { label, hint, error, className, id, type, revealable, ...rest },
  ref,
) {
  const generated = useId();
  const inputId = id ?? generated;
  const describedBy = error ? `${inputId}-error` : hint ? `${inputId}-hint` : undefined;
  const [revealed, setRevealed] = useState(false);

  const actualType = revealable && revealed ? "text" : type;

  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="block text-sm font-medium text-ink-200">
        {label}
      </label>

      <div className="relative">
        <input
          ref={ref}
          id={inputId}
          type={actualType}
          // Screen readers announce the message only if the input points at it.
          aria-describedby={describedBy}
          aria-invalid={error ? true : undefined}
          className={cx(
            "w-full rounded-lg border bg-ink-900/70 px-3.5 py-2.5 text-[0.95rem] text-ink-100",
            "placeholder:text-ink-500 transition-all duration-150",
            "focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none",
            "disabled:cursor-not-allowed disabled:opacity-60",
            revealable && "pr-11",
            error ? "border-err-500/70" : "border-ink-600/70 hover:border-ink-500",
            className,
          )}
          {...rest}
        />

        {revealable && (
          <button
            type="button"
            // Not in the tab order: it is a convenience, and stopping between
            // the password field and the submit button is worse than useful.
            tabIndex={-1}
            onClick={() => setRevealed((value) => !value)}
            aria-label={revealed ? "Hide password" : "Show password"}
            className="absolute top-1/2 right-3 -translate-y-1/2 text-ink-500 transition-colors hover:text-ink-200"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden="true">
              {revealed ? (
                <path
                  d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.4 5.4A9.8 9.8 0 0112 5c5 0 9 4.5 9 7a12 12 0 01-2.4 3.3M6.2 6.2A12.6 12.6 0 003 12c0 2.5 4 7 9 7 1.2 0 2.3-.2 3.3-.6"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                />
              ) : (
                <>
                  <path
                    d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"
                    stroke="currentColor"
                    strokeWidth="1.6"
                  />
                  <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.6" />
                </>
              )}
            </svg>
          </button>
        )}
      </div>

      {error ? (
        <motion.p
          id={`${inputId}-error`}
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="text-sm text-err-500"
        >
          {error}
        </motion.p>
      ) : hint ? (
        <p id={`${inputId}-hint`} className="text-sm text-ink-400">
          {hint}
        </p>
      ) : null}
    </div>
  );
});

/* --------------------------------------------------------------- alert --- */

export function Alert({
  tone = "error",
  children,
}: {
  tone?: "error" | "success" | "info";
  children: ReactNode;
}) {
  const tones = {
    error: "border-err-500/35 bg-err-500/10 text-err-500",
    success: "border-ok-500/35 bg-ok-500/10 text-ok-500",
    info: "border-brand-400/35 bg-brand-500/10 text-brand-300",
  } as const;

  const icons = {
    error: "M12 8v5M12 16.5v.5M12 3l9 16H3l9-16Z",
    success: "M4.5 12.5l5 5 10-10",
    info: "M12 11v6M12 7.5v.5M12 3a9 9 0 100 18 9 9 0 000-18Z",
  } as const;

  return (
    <motion.div
      initial={{ opacity: 0, y: -6, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.2 }}
      // Errors that appear after an action must be announced, not just drawn.
      role={tone === "error" ? "alert" : "status"}
      className={cx("flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-sm", tones[tone])}
    >
      <svg viewBox="0 0 24 24" className="mt-0.5 h-4 w-4 flex-none" fill="none" aria-hidden="true">
        <path
          d={icons[tone]}
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="min-w-0">{children}</span>
    </motion.div>
  );
}

/* -------------------------------------------------------------- layout --- */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("surface rounded-xl", className)}>{children}</div>;
}

export function Badge({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full border border-ink-600/60",
        "bg-ink-850/60 px-3 py-1 text-xs font-medium text-ink-300",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Logo({ className }: { className?: string }) {
  // The gradient id must be unique per instance. It was a literal "bx", which
  // is fine until two logos are on the page at once — the sidebar and the
  // mobile drawer. Duplicate ids collapse to one target, and every `url(#bx)`
  // in the document resolves to whichever came first; when that one sat inside
  // a hidden element the other logo rendered as nothing at all.
  const gradient = useId();
  return (
    <span className={cx("inline-flex items-center gap-2.5", className)}>
      <svg viewBox="0 0 32 32" className="h-7 w-7" aria-hidden="true">
        <defs>
          <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--series)" />
            <stop offset="100%" stopColor="var(--color-accent-500)" />
          </linearGradient>
        </defs>
        <rect x="2" y="2" width="28" height="28" rx="9" fill={`url(#${gradient})`} opacity="0.16" />
        <circle cx="10" cy="10" r="3" fill={`url(#${gradient})`} />
        <circle cx="22" cy="10" r="3" fill={`url(#${gradient})`} />
        <circle cx="16" cy="22" r="3" fill={`url(#${gradient})`} />
        <path
          d="M10 13v3a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-3"
          stroke={`url(#${gradient})`}
          strokeWidth="1.8"
          fill="none"
          strokeLinecap="round"
        />
      </svg>
      <span className="text-[1.05rem] font-semibold tracking-tight text-ink-100">Basivo</span>
    </span>
  );
}
