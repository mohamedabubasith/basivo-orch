import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { cx } from "../../lib/cx";
import { Logo, PageLoader } from "../../components/ui";

/**
 * Gate for everything behind sign-in.
 *
 * This is a routing convenience, not a security boundary. Every protected
 * endpoint enforces its own authentication server-side; hiding the UI just
 * spares the user a screen full of failed requests. Anyone who edits this
 * component in devtools gets an empty shell and 401s.
 */
export function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();

  // Held, not flashed. The session probe is one request; rendering the
  // signed-out view first and correcting it a moment later shows a login
  // screen to someone who is already signed in.
  if (loading) return <PageLoader label="Checking your session" />;

  if (!user) {
    // Remember where they were going so sign-in can put them back.
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <Outlet />;
}

const NAV = [
  { to: "/app", label: "Overview", end: true },
  { to: "/app/security", label: "Security" },
];

export function AppShell() {
  const { user, signOut } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setMenuOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menuOpen]);

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-40 border-b border-ink-800/70 bg-ink-950/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-5">
          <div className="flex items-center gap-6">
            <Logo />

            <nav className="flex items-center gap-1">
              {NAV.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end} className="relative">
                  {({ isActive }) => (
                    <span
                      className={cx(
                        "relative block rounded-lg px-3 py-1.5 text-sm transition-colors",
                        isActive ? "text-ink-100" : "text-ink-400 hover:text-ink-200",
                      )}
                    >
                      {/* One element that travels between tabs, rather than a
                          background per tab fading in and out. `layoutId` is
                          what makes it move instead of teleport. */}
                      {isActive && (
                        <motion.span
                          layoutId="nav-active"
                          className="absolute inset-0 rounded-lg bg-ink-800"
                          transition={{ type: "spring", stiffness: 380, damping: 30 }}
                        />
                      )}
                      <span className="relative">{item.label}</span>
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>
          </div>

          <div className="relative flex items-center gap-3" ref={menuRef}>
            <button
              onClick={() => setMenuOpen((open) => !open)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              className="flex items-center gap-2 rounded-lg py-1 pr-2 pl-1 transition-colors hover:bg-ink-850"
            >
              <span className="grid h-8 w-8 place-items-center rounded-full bg-gradient-to-br from-brand-500 to-accent-500 text-sm font-semibold text-white">
                {(user?.email ?? "?").charAt(0).toUpperCase()}
              </span>
              <span className="hidden max-w-[12rem] truncate text-sm text-ink-300 sm:inline">
                {user?.email}
              </span>
              <svg
                viewBox="0 0 24 24"
                className="h-4 w-4 text-ink-500"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M6 9l6 6 6-6"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
            </button>

            <AnimatePresence>
              {menuOpen && (
                <motion.div
                  role="menu"
                  initial={{ opacity: 0, y: -6, scale: 0.97 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -6, scale: 0.97 }}
                  transition={{ duration: 0.15 }}
                  className="surface absolute top-full right-0 mt-2 w-56 rounded-xl p-1.5 shadow-xl shadow-black/40"
                >
                  <div className="border-b border-ink-700/60 px-3 py-2">
                    <p className="truncate text-sm text-ink-200">{user?.email}</p>
                    <p className="mt-0.5 text-xs text-ink-500">
                      {user?.is_verified ? "Email confirmed" : "Email unconfirmed"}
                    </p>
                  </div>
                  <button
                    role="menuitem"
                    onClick={() => void signOut()}
                    className="mt-1 w-full rounded-lg px-3 py-2 text-left text-sm text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100"
                  >
                    Sign out
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-10">
        <Outlet />
      </main>
    </div>
  );
}
