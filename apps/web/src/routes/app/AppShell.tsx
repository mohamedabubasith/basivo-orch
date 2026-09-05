/**
 * The application chrome: a left sidebar, the shape every tool of this kind
 * uses, and for a reason worth stating — a horizontal bar has room for about
 * five words before it starts hiding things behind a "More" menu, and this app
 * has a workspace switcher, five destinations and an account to fit. Vertical
 * space is the one axis that does not run out.
 *
 * Three states are resolved here, in order, so that no page below has to think
 * about any of them: not signed in, signed in but unconfirmed, signed in with
 * no workspace. Each is a whole screen rather than a message wedged into a
 * dashboard that cannot load.
 */

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Navigate, Outlet, useLocation } from "react-router-dom";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { loadConfig } from "../../lib/config";
import { cx } from "../../lib/cx";
import { WorkspaceProvider, useWorkspace } from "../../lib/workspace";
import { Alert, Button, Field, Logo, PageLoader } from "../../components/ui";
import { ThemeToggle } from "../../components/ThemeToggle";

/* ------------------------------------------------------------------ gates --- */

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

/**
 * The mirror of `RequireAuth`: a signed-in visitor has no use for the sign-in
 * page.
 *
 * Cookies are shared across tabs, so opening /login in a second tab was always
 * *authenticated* — the route simply never looked, and rendered the form to
 * someone who was already in. Which reads exactly like being signed out.
 *
 * `loading` is held rather than flashed for the same reason it is in
 * `RequireAuth`: rendering the form during the session probe and correcting it
 * a moment later is the bug, not the fix.
 */
export function RedirectIfSignedIn() {
  const { user, loading } = useAuth();

  if (loading) return <PageLoader label="Checking your session" />;
  if (user) return <Navigate to="/app" replace />;
  return <Outlet />;
}

/**
 * Gate for a confirmed email address.
 *
 * Mirrors the server rule rather than restating it — `require_verified_email`
 * comes from `GET /config`, so a deployment that relaxes the gate does not end
 * up with a UI still enforcing it. The API is the boundary either way; this
 * only decides whether the user meets a wall or a 403.
 */
export function RequireVerified() {
  const { user } = useAuth();
  const [required, setRequired] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    void loadConfig().then((config) => {
      if (!cancelled) setRequired(config.require_verified_email);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (required === null) return <PageLoader label="Loading" />;
  if (required && user && !user.is_verified)
    return <Navigate to="/confirm-email" replace />;
  return <Outlet />;
}

/* --------------------------------------------------------------- nav data --- */

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
  icon: ReactNode;
}

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const NAV: { heading: string; items: NavItem[] }[] = [
  {
    heading: "Workspace",
    items: [
      {
        to: "/app",
        label: "Overview",
        end: true,
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <path d="M4 13h6V4H4v9ZM14 20h6v-9h-6v9ZM4 20h6v-3H4v3ZM14 7h6V4h-6v3Z" />
          </svg>
        ),
      },
      {
        to: "/app/flows",
        label: "Flows",
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="18" cy="12" r="2.5" />
            <circle cx="6" cy="18" r="2.5" />
            <path d="M8.5 6h3a2 2 0 0 1 2 2v2M8.5 18h3a2 2 0 0 0 2-2v-2" />
          </svg>
        ),
      },
      {
        to: "/app/runs",
        label: "Runs",
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <circle cx="12" cy="12" r="8.5" />
            <path d="M12 7.5V12l3 2" />
          </svg>
        ),
      },
      {
        to: "/app/skills",
        label: "Skills",
        icon: (
          // An open book: the agent looks something up.
          <svg viewBox="0 0 24 24" {...stroke}>
            <path d="M12 6.5C10.5 5.2 8.4 4.7 5 5v13c3.4-.3 5.5.2 7 1.5 1.5-1.3 3.6-1.8 7-1.5V5c-3.4-.3-5.5.2-7 1.5Z" />
            <path d="M12 6.5v13" />
          </svg>
        ),
      },
    ],
  },
  {
    heading: "Account",
    items: [
      {
        to: "/app/api-keys",
        label: "API keys",
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <circle cx="8" cy="12" r="3.5" />
            <path d="M11.5 12H21M18 12v3M15 12v2" />
          </svg>
        ),
      },
      {
        to: "/app/credentials",
        label: "Credentials",
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <rect x="4" y="10.5" width="16" height="9" rx="2" />
            <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
          </svg>
        ),
      },
      {
        to: "/app/security",
        label: "Security",
        icon: (
          <svg viewBox="0 0 24 24" {...stroke}>
            <path d="M12 3.5 5 6.5v5c0 4 2.9 7.6 7 9 4.1-1.4 7-5 7-9v-5l-7-3Z" />
            <path d="M9.3 12.2 11.2 14l3.5-3.6" />
          </svg>
        ),
      },
    ],
  },
];

/* ------------------------------------------------------------------ shell --- */

const COLLAPSE_KEY = "basivo.sidebar.collapsed";

export function AppShell() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();

  // A drawer that survives navigation covers the page the user just asked for.
  useEffect(() => setDrawerOpen(false), [location.pathname]);

  function toggleCollapsed() {
    setCollapsed((value) => {
      try {
        localStorage.setItem(COLLAPSE_KEY, value ? "0" : "1");
      } catch {
        // Preference only; a refusal here must not break the layout.
      }
      return !value;
    });
  }

  return (
    <WorkspaceProvider>
      <div className="min-h-dvh lg:flex">
        {/* Desktop: a real column, so the content beside it scrolls on its own
            and the nav never scrolls out of reach. */}
        <motion.aside
          animate={{ width: collapsed ? 76 : 264 }}
          transition={{ type: "spring", stiffness: 420, damping: 38 }}
          className="sticky top-0 hidden h-dvh flex-none border-r border-ink-800/70 bg-ink-900/40 lg:block"
        >
          <SidebarContent collapsed={collapsed} onToggle={toggleCollapsed} />
        </motion.aside>

        {/* Mobile: the same content in a drawer. */}
        <AnimatePresence>
          {drawerOpen && (
            <>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                onClick={() => setDrawerOpen(false)}
                className="fixed inset-0 z-40 bg-ink-950/70 backdrop-blur-sm lg:hidden"
              />
              <motion.aside
                initial={{ x: -280 }}
                animate={{ x: 0 }}
                exit={{ x: -280 }}
                transition={{ type: "spring", stiffness: 420, damping: 40 }}
                className="fixed inset-y-0 left-0 z-50 w-[264px] border-r border-ink-800/70 bg-ink-900 lg:hidden"
              >
                <SidebarContent collapsed={false} />
              </motion.aside>
            </>
          )}
        </AnimatePresence>

        <div className="min-w-0 flex-1">
          {/* Mobile only: something has to open the drawer. */}
          <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-ink-800/70 bg-ink-950/85 px-4 backdrop-blur-xl lg:hidden">
            <button
              onClick={() => setDrawerOpen(true)}
              aria-label="Open navigation"
              className="rounded-lg p-2 text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100"
            >
              <svg viewBox="0 0 24 24" className="h-5 w-5" {...stroke}>
                <path d="M4 7h16M4 12h16M4 17h16" />
              </svg>
            </button>
            <Logo />
          </header>

          <main className="relative mx-auto max-w-6xl px-5 py-8 sm:px-8 sm:py-10">
            <WorkspaceGate />
          </main>
        </div>
      </div>
    </WorkspaceProvider>
  );
}

/**
 * No workspace means every page below is addressing an id that does not exist.
 * Resolving it here rather than per-page is why the dashboard could once render
 * completely blank: it finished loading, found nothing, and had no branch for
 * the case.
 */
function WorkspaceGate() {
  const { loading, current, error, refresh, select } = useWorkspace();
  const location = useLocation();

  if (loading) return <PageLoader label="Loading your workspace" />;
  if (error && !current) {
    return (
      <Alert>
        {error}{" "}
        <button
          onClick={() => void refresh()}
          className="underline underline-offset-2"
        >
          Try again
        </button>
      </Alert>
    );
  }
  if (!current) {
    return (
      <CreateWorkspace
        onCreated={async (id) => {
          await refresh();
          select(id);
        }}
      />
    );
  }
  // Only the page content is keyed, so the sidebar, the workspace provider and
  // its data survive navigation. `mode="wait"` would hold an empty frame
  // between pages; overlapping the fade keeps the chrome visibly still.
  return (
    <AnimatePresence initial={false}>
      <motion.div
        key={location.pathname}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: [0.21, 0.5, 0.35, 1] }}
      >
        <Outlet />
      </motion.div>
    </AnimatePresence>
  );
}

/* ---------------------------------------------------------------- sidebar --- */

function SidebarContent({
  collapsed,
  onToggle,
}: {
  collapsed: boolean;
  onToggle?: () => void;
}) {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div
        className={cx(
          "flex h-14 flex-none items-center",
          collapsed ? "justify-center" : "px-5",
        )}
      >
        {collapsed ? <Logo className="[&>span:last-child]:hidden" /> : <Logo />}
      </div>

      {!collapsed && <WorkspaceSwitcher />}

      <nav className="mt-1 flex-1 overflow-y-auto px-3">
        {NAV.map((group) => (
          <div key={group.heading} className="mb-3.5">
            {!collapsed && (
              <p className="mb-1 px-3 text-[0.7rem] font-medium tracking-[0.14em] text-ink-400 uppercase">
                {group.heading}
              </p>
            )}
            <ul className="space-y-0.5">
              {group.items.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    title={collapsed ? item.label : undefined}
                  >
                    {({ isActive }) => (
                      <span
                        className={cx(
                          "relative flex items-center rounded-lg py-1.5 text-[0.86rem] transition-colors",
                          collapsed ? "justify-center px-2" : "gap-3 px-3",
                          isActive
                            ? "text-ink-100"
                            : "text-ink-300 hover:text-ink-100",
                        )}
                      >
                        {/* One element that travels between items, rather than
                            a background per item fading in and out. `layoutId`
                            is what makes it move instead of teleport.
                            No border and no separate dot: the dot sat OUTSIDE
                            this pill, at -left-1, so it read as a stray mark
                            floating beside the sidebar rather than as an
                            indicator of anything. The fill alone says which
                            item is current. */}
                        {isActive && (
                          <motion.span
                            layoutId="nav-active"
                            className="absolute inset-0 rounded-lg bg-brand-500/[0.14] ring-1 ring-brand-400/25"
                            transition={{
                              type: "spring",
                              stiffness: 400,
                              damping: 34,
                            }}
                          />
                        )}
                        <span
                          className={cx(
                            "relative h-[18px] w-[18px] flex-none transition-colors",
                            isActive && "text-brand-400",
                          )}
                        >
                          {item.icon}
                        </span>
                        {!collapsed && (
                          <span className="relative truncate">
                            {item.label}
                          </span>
                        )}
                      </span>
                    )}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <AccountBlock collapsed={collapsed} />

      <div
        className={cx(
          "flex flex-none items-center border-t border-ink-800/70 px-3 py-2",
          collapsed ? "justify-center" : "justify-between",
        )}
      >
        {!collapsed && <ThemeToggle compact />}
        {onToggle && (
          <button
            onClick={onToggle}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="rounded-lg p-2 text-ink-500 transition-colors hover:bg-ink-850 hover:text-ink-200"
          >
            <svg
              viewBox="0 0 24 24"
              className={cx(
                "h-4 w-4 transition-transform",
                collapsed && "rotate-180",
              )}
              {...stroke}
            >
              <path d="M14 7l-5 5 5 5" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

function WorkspaceSwitcher() {
  const { workspaces, current, select } = useWorkspace();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useCloseOnOutside(ref, open, () => setOpen(false));

  return (
    <div className="relative px-3" ref={ref}>
      <button
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-ink-850"
      >
        <span className="grid h-6 w-6 flex-none place-items-center rounded-md bg-gradient-to-br from-brand-500 to-accent-500 text-xs font-semibold text-white">
          {(current?.name ?? "?").charAt(0).toUpperCase()}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-ink-100">
            {current?.name}
          </span>
          <span className="block truncate text-xs text-ink-500 capitalize">
            {current?.role}
          </span>
        </span>
        <svg
          viewBox="0 0 24 24"
          className="h-4 w-4 flex-none text-ink-500"
          {...stroke}
        >
          <path d="M8 10l4-4 4 4M8 14l4 4 4-4" />
        </svg>
      </button>

      <AnimatePresence>
        {open && (
          <motion.ul
            role="listbox"
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.98 }}
            transition={{ duration: 0.14 }}
            className="surface absolute inset-x-3 top-full z-20 mt-1.5 max-h-64 overflow-y-auto rounded-xl p-1.5 shadow-xl shadow-black/50"
          >
            {workspaces.map((workspace) => (
              <li key={workspace.id}>
                <button
                  role="option"
                  aria-selected={workspace.id === current?.id}
                  onClick={() => {
                    select(workspace.id);
                    setOpen(false);
                  }}
                  className={cx(
                    "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors hover:bg-ink-800",
                    workspace.id === current?.id
                      ? "text-ink-100"
                      : "text-ink-300",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">
                    {workspace.name}
                  </span>
                  {workspace.id === current?.id && (
                    <svg
                      viewBox="0 0 12 12"
                      className="h-3.5 w-3.5 flex-none text-brand-400"
                    >
                      <path
                        d="M2.5 6.4 4.8 8.7 9.5 3.9"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                </button>
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}

function AccountBlock({ collapsed }: { collapsed: boolean }) {
  const { user, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useCloseOnOutside(ref, open, () => setOpen(false));

  return (
    <div
      className="relative flex-none border-t border-ink-800/70 p-3"
      ref={ref}
    >
      <button
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={collapsed ? user?.email : undefined}
        className={cx(
          "flex w-full items-center rounded-xl py-2 transition-colors hover:bg-ink-850",
          collapsed ? "justify-center px-1" : "gap-2.5 px-2",
        )}
      >
        <span className="grid h-8 w-8 flex-none place-items-center rounded-full bg-gradient-to-br from-brand-500 to-accent-500 text-sm font-semibold text-white">
          {(user?.email ?? "?").charAt(0).toUpperCase()}
        </span>
        {!collapsed && (
          <>
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-sm text-ink-200">
                {user?.email}
              </span>
              <span
                className="block text-xs"
                style={{ color: "var(--status-good)" }}
              >
                {user?.is_verified ? "Email confirmed" : "Email unconfirmed"}
              </span>
            </span>
            <svg
              viewBox="0 0 24 24"
              className="h-4 w-4 flex-none text-ink-500"
              {...stroke}
            >
              <path d="M6 9l6 6 6-6" />
            </svg>
          </>
        )}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            initial={{ opacity: 0, y: 6, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 6, scale: 0.97 }}
            transition={{ duration: 0.14 }}
            className="surface absolute right-3 bottom-full left-3 z-20 mb-1.5 rounded-xl p-1.5 shadow-xl shadow-black/50"
          >
            <Link
              to="/app/security"
              role="menuitem"
              onClick={() => setOpen(false)}
              className="block rounded-lg px-3 py-2 text-sm text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100"
            >
              Security
            </Link>
            <button
              role="menuitem"
              onClick={() => void signOut()}
              className="w-full rounded-lg px-3 py-2 text-left text-sm text-ink-300 transition-colors hover:bg-ink-800 hover:text-ink-100"
            >
              Sign out
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function useCloseOnOutside(
  ref: React.RefObject<HTMLElement | null>,
  active: boolean,
  close: () => void,
) {
  useEffect(() => {
    if (!active) return;
    const onDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) close();
    };
    const onEscape = (event: KeyboardEvent) =>
      event.key === "Escape" && close();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onEscape);
    };
  }, [ref, active, close]);
}

/* -------------------------------------------------------- first workspace --- */

function CreateWorkspace({
  onCreated,
}: {
  onCreated: (id: string) => void | Promise<void>;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const slug =
        name
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-|-$/g, "")
          .slice(0, 40) || "workspace";
      const created = await api.post<{ id: string }>("/orgs", {
        name: name.trim(),
        slug,
      });
      await onCreated(created.id);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not create the workspace.",
      );
      setBusy(false);
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="mx-auto max-w-md py-10"
    >
      <h1 className="text-2xl font-semibold tracking-tight text-ink-100">
        Create your workspace
      </h1>
      <p className="mt-2 leading-relaxed text-ink-400">
        Flows, runs and API keys all live inside a workspace, so there is
        nothing to show until you have one. You can rename it later.
      </p>

      <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        <Field
          label="Workspace name"
          name="workspace"
          required
          autoFocus
          placeholder="Acme Engineering"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <Button type="submit" loading={busy} disabled={!name.trim()}>
          Create workspace
        </Button>
      </form>
    </motion.div>
  );
}
