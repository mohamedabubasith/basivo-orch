/**
 * Where the app lives, when the landing page lives somewhere else.
 *
 * The two hostnames are separate origins, and a session cookie belongs to
 * exactly one of them. Registering on the marketing host therefore produced a
 * session the console could not see: the user signed up, went to the console,
 * and was asked to sign in again — with no way to tell that they were already
 * signed in *next door*.
 *
 * So the app has one home. Anything that opens it points here absolutely, and
 * `useConsoleGuard` sends an app route that somehow renders on the other host
 * over to this one. Empty when the deployment serves everything from a single
 * hostname, where all of this is a no-op.
 */
const CONFIGURED = (import.meta.env.VITE_CONSOLE_ORIGIN ?? "").replace(
  /\/$/,
  "",
);

/** The console's origin, or "" when this deployment has only one hostname. */
export function consoleOrigin(): string {
  if (!CONFIGURED) return "";
  if (typeof window === "undefined") return CONFIGURED;
  // Already on it: relative links keep working and no redirect is needed.
  return window.location.origin === CONFIGURED ? "" : CONFIGURED;
}

/** An absolute URL for an app route, or the plain path when already home. */
export function consoleUrl(path: string): string {
  const origin = consoleOrigin();
  return origin ? origin + path : path;
}

/** Whether this path is part of the application rather than the landing page. */
export function isAppRoute(path: string): boolean {
  return /^\/(app|login|register|two-factor|forgot-password|confirm-email)(\/|$)/.test(
    path,
  );
}
