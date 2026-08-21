import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Backdrop } from "../../components/Backdrop";
import { Logo } from "../../components/ui";

/**
 * One composition for every auth screen: a single centered card on a
 * decorated backdrop.
 *
 * The previous layout was a split screen — an animated product demo on the
 * left, a bare form floating on the page to the right. Split-screen auth is
 * the 2019 SaaS template; worse, the form side had no container at all, so it
 * read as unfinished rather than minimal. A centered card is what every tool
 * this product is compared against trains people to expect: one column, one
 * card, the logo above it, nothing competing with the form for attention.
 * The product demo belongs on the landing page, which already has it.
 */
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
    <div className="relative flex min-h-dvh flex-col overflow-hidden">
      <Backdrop />

      <main className="relative flex flex-1 items-center justify-center px-5 py-12">
        <motion.div
          className="w-full max-w-[26rem]"
          initial={reduceMotion ? false : { opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: [0.21, 0.5, 0.35, 1] }}
        >
          <div className="mb-6 flex justify-center">
            <Link to="/" className="rounded-lg" aria-label="Basivo home">
              <Logo />
            </Link>
          </div>

          <div className="surface rounded-2xl p-7 sm:p-8">
            <div className="mb-6">
              <h1 className="text-[1.45rem] leading-tight font-semibold tracking-tight text-ink-100">
                {title}
              </h1>
              {subtitle && (
                <p className="mt-1.5 text-sm leading-relaxed text-ink-400">
                  {subtitle}
                </p>
              )}
            </div>

            {children}
          </div>

          {footer && (
            <div className="mt-6 text-center text-sm text-ink-400">
              {footer}
            </div>
          )}
        </motion.div>
      </main>
    </div>
  );
}
