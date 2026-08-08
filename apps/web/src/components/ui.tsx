import { motion } from "motion/react";
import {
  forwardRef,
  useId,
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
    "relative inline-flex items-center justify-center gap-2 rounded-xl font-medium " +
    "transition-all duration-200 disabled:cursor-not-allowed disabled:opacity-55 " +
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";

  const variants = {
    primary:
      "bg-brand-500 text-white shadow-lg shadow-brand-500/25 " +
      "hover:bg-brand-400 hover:shadow-brand-500/40 active:scale-[0.985]",
    secondary:
      "surface text-ink-100 hover:border-ink-500 hover:bg-ink-800/80 active:scale-[0.985]",
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
      {loading && <Spinner />}
      <span className={cx(loading && "opacity-0")}>{children}</span>
      {loading && <span className="absolute inset-0 grid place-items-center"><Spinner /></span>}
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
      <path
        className="opacity-90"
        fill="currentColor"
        d="M4 12a8 8 0 0 1 8-8v3a5 5 0 0 0-5 5H4z"
      />
    </svg>
  );
}

/* --------------------------------------------------------------- field --- */

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: ReactNode;
  error?: string | null;
};

export const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { label, hint, error, className, id, ...rest },
  ref,
) {
  const generated = useId();
  const inputId = id ?? generated;
  const describedBy = error ? `${inputId}-error` : hint ? `${inputId}-hint` : undefined;

  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="block text-sm font-medium text-ink-200">
        {label}
      </label>
      <input
        ref={ref}
        id={inputId}
        // Screen readers announce the message only if the input points at it.
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        className={cx(
          "w-full rounded-xl border bg-ink-900/70 px-3.5 py-2.5 text-[0.95rem] text-ink-100",
          "placeholder:text-ink-500 transition-colors duration-150",
          "focus:border-brand-400 focus:bg-ink-900 focus:outline-none",
          error ? "border-err-500/70" : "border-ink-600/70 hover:border-ink-500",
          className,
        )}
        {...rest}
      />
      {error ? (
        <p id={`${inputId}-error`} className="text-sm text-err-500">
          {error}
        </p>
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

  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      // Errors that appear after an action must be announced, not just drawn.
      role={tone === "error" ? "alert" : "status"}
      className={cx("rounded-xl border px-3.5 py-2.5 text-sm", tones[tone])}
    >
      {children}
    </motion.div>
  );
}

/* -------------------------------------------------------------- layout --- */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("surface rounded-2xl", className)}>{children}</div>;
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
  return (
    <span className={cx("inline-flex items-center gap-2.5", className)}>
      <svg viewBox="0 0 32 32" className="h-7 w-7" aria-hidden="true">
        <defs>
          <linearGradient id="bx" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#818cf8" />
            <stop offset="100%" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
        <rect x="2" y="2" width="28" height="28" rx="9" fill="url(#bx)" opacity="0.16" />
        <circle cx="10" cy="10" r="3" fill="url(#bx)" />
        <circle cx="22" cy="10" r="3" fill="url(#bx)" />
        <circle cx="16" cy="22" r="3" fill="url(#bx)" />
        <path
          d="M10 13v3a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-3"
          stroke="url(#bx)"
          strokeWidth="1.8"
          fill="none"
          strokeLinecap="round"
        />
      </svg>
      <span className="text-[1.05rem] font-semibold tracking-tight text-ink-100">
        Basivo
      </span>
    </span>
  );
}
