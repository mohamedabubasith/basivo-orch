/**
 * Server decisions the browser has to mirror.
 *
 * Fetched rather than hard-coded, because a duplicated rule is a rule that can
 * disagree with itself. If the UI decided on its own whether email
 * confirmation gates the app, one deployment would show a wall the API does
 * not enforce and another would hand out 403s the UI never anticipated. The
 * API decides; this reads the decision.
 */

const API_BASE = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export interface PublicConfig {
  app_name: string;
  version: string;
  require_verified_email: boolean;
  /** Where a published flow answers — the server's own idea of its address. */
  public_base_url?: string;
}

const FALLBACK: PublicConfig = {
  app_name: "Basivo",
  version: "unknown",
  require_verified_email: true,
};

let cached: Promise<PublicConfig> | null = null;

export function loadConfig(): Promise<PublicConfig> {
  cached ??= fetch(`${API_BASE}/config`, { credentials: "include" })
    .then((response) => (response.ok ? (response.json() as Promise<PublicConfig>) : FALLBACK))
    // Fail closed. If the config cannot be read, assume the stricter rule: a
    // wall that turns out to be unnecessary is a nuisance, whereas skipping a
    // wall that was real means every page behind it answers 403 instead.
    .catch(() => FALLBACK);
  return cached;
}
