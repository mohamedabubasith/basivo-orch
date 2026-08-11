import { NavLink, Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { cx } from "../../lib/cx";
import { Button, Logo, PageLoader } from "../../components/ui";

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

  // Held, not flashed. The session probe is one request; rendering the signed
  // -out view first and correcting it a moment later shows a login screen to
  // someone who is already signed in.
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

  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-40 border-b border-ink-800/70 bg-ink-950/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-5">
          <div className="flex items-center gap-7">
            <Logo />
            <nav className="flex items-center gap-1">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cx(
                      "rounded-lg px-3 py-1.5 text-sm transition-colors",
                      isActive
                        ? "bg-ink-800 text-ink-100"
                        : "text-ink-400 hover:bg-ink-850 hover:text-ink-200",
                    )
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>

          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-ink-400 sm:inline">{user?.email}</span>
            <Button variant="secondary" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-5 py-10">
        <Outlet />
      </main>
    </div>
  );
}
