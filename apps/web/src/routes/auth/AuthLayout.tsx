import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Logo } from "../../components/ui";
import { AuthAside } from "./AuthAside";

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
    <div className="grid min-h-dvh lg:grid-cols-[1fr_minmax(0,34rem)]">
      {/* The panel is hidden below lg rather than stacked. On a phone it would
          push the form below the fold, and nobody scrolls past decoration to
          reach a login box. */}
      <AuthAside />

      <div className="relative flex flex-col">
        <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden lg:hidden">
          <div className="grid-bg absolute inset-0 opacity-[0.12] [mask-image:radial-gradient(ellipse_at_50%_0%,black_20%,transparent_70%)]" />
          <div className="absolute -top-24 left-1/2 h-72 w-96 -translate-x-1/2 rounded-full bg-brand-500/14 blur-[90px]" />
        </div>

        <header className="relative px-6 py-6 sm:px-10">
          <Link to="/" className="inline-block rounded-lg" aria-label="Basivo home">
            <Logo />
          </Link>
        </header>

        <main className="relative flex flex-1 items-center justify-center px-6 pb-16 sm:px-10">
          <motion.div
            className="w-full max-w-[24rem]"
            initial={reduceMotion ? false : { opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.45, ease: [0.21, 0.5, 0.35, 1] }}
          >
            <div className="mb-7">
              <motion.h1
                className="text-[1.75rem] leading-tight font-semibold tracking-tight text-ink-100"
                initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.4, delay: 0.05 }}
              >
                {title}
              </motion.h1>
              {subtitle && (
                <motion.p
                  className="mt-2 text-[0.95rem] text-ink-400"
                  initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.4, delay: 0.1 }}
                >
                  {subtitle}
                </motion.p>
              )}
            </div>

            <motion.div
              initial={reduceMotion ? false : { opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4, delay: 0.15 }}
            >
              {children}
            </motion.div>

            {footer && <div className="mt-7 text-center text-sm text-ink-400">{footer}</div>}
          </motion.div>
        </main>
      </div>
    </div>
  );
}
