import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Logo } from "../../components/ui";

export function AuthLayout({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const reduceMotion = useReducedMotion();

  return (
    <div className="relative flex min-h-dvh flex-col">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="grid-bg absolute inset-0 opacity-[0.14] [mask-image:radial-gradient(ellipse_at_50%_20%,black_20%,transparent_65%)]" />
        <div className="absolute -top-32 left-1/2 h-[420px] w-[680px] -translate-x-1/2 rounded-full bg-brand-500/14 blur-[110px]" />
      </div>

      <header className="relative px-5 py-6">
        <Link to="/" className="inline-block rounded-lg" aria-label="Basivo home">
          <Logo />
        </Link>
      </header>

      <main className="relative flex flex-1 items-start justify-center px-5 pt-6 pb-16">
        <motion.div
          className="w-full max-w-[26rem]"
          initial={reduceMotion ? false : { opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: [0.21, 0.5, 0.35, 1] }}
        >
          <div className="mb-7 text-center">
            <h1 className="text-2xl font-semibold tracking-tight text-ink-100">{title}</h1>
            {subtitle && <p className="mt-2 text-[0.95rem] text-ink-400">{subtitle}</p>}
          </div>

          <div className="surface rounded-2xl p-6 sm:p-7">{children}</div>

          {footer && <div className="mt-6 text-center text-sm text-ink-400">{footer}</div>}
        </motion.div>
      </main>
    </div>
  );
}
