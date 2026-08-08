/**
 * The single way this app talks to the API.
 *
 * Three things it exists to get right, none of which should be re-implemented
 * per call site:
 *
 *  1. **No token ever touches JavaScript storage.** The session and refresh
 *     tokens are HttpOnly cookies set by the API. Nothing here reads them, and
 *     nothing here puts a credential in localStorage — that is the difference
 *     between an XSS bug leaking a rendered page and an XSS bug leaking a
 *     durable session.
 *
 *  2. **CSRF on every mutating request.** The API uses double-submit: a
 *     non-HttpOnly cookie whose value must be echoed in `X-CSRF-Token`.
 *
 *  3. **Refresh is single-flight.** See `refresh()` — this one is load-bearing.
 */

export const CSRF_HEADER = "X-CSRF-Token";

/**
 * The API lives on its own origin (see vite.config.ts for why there is no
 * proxy). Requests therefore carry `credentials: "include"`, and the API's
 * CORS_ORIGINS must list this app's origin for any of it to work.
 */
const API_BASE = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Paths that bootstrap a session and so cannot present a session-based retry. */
const NO_REFRESH_RETRY = [
  "/auth/login",
  "/auth/refresh",
  "/auth/register",
  "/auth/forgot-password",
  "/auth/reset-password",
  "/auth/verify",
  "/auth/2fa/verify",
];

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryAfter?: number;

  constructor(status: number, message: string, code?: string, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }

  /** True when the user can fix this by changing what they typed. */
  get isUserFixable(): boolean {
    return this.status === 400 || this.status === 422 || this.status === 409;
  }
}

/** Raised when the first factor passed but a second is outstanding. */
export class StepUpRequired extends Error {
  readonly token: string;
  readonly methods: string[];

  constructor(token: string, methods: string[]) {
    super("Two-factor authentication required.");
    this.name = "StepUpRequired";
    this.token = token;
    this.methods = methods;
  }
}

/**
 * The CSRF token, held in memory.
 *
 * Double-submit needs the same value in a cookie and in a header. The cookie
 * half is the browser's job and happens automatically. For the header half we
 * read `X-CSRF-Token` off responses rather than parsing `document.cookie`,
 * because the cookie is host-only to the API's domain — readable from
 * JavaScript on localhost, but not from app.example.com when the API is on
 * api.example.com. The response header is exposed via CORS and works in both.
 *
 * Not a secret: its protection comes from the same-origin policy, which stops
 * a cross-site attacker from reading our responses to learn the value.
 */
let csrfToken: string | null = null;

function captureCsrf(response: Response): void {
  const fresh = response.headers.get(CSRF_HEADER);
  // Every sign-in rotates the token, so always take the newest one.
  if (fresh) csrfToken = fresh;
}

async function ensureCsrf(): Promise<string | null> {
  if (csrfToken) return csrfToken;
  const response = await fetch(`${API_BASE}/auth/csrf`, { credentials: "include" });
  captureCsrf(response);
  return csrfToken;
}

/**
 * Single-flight refresh.
 *
 * The API rotates refresh tokens and treats a *replayed* one as a compromise
 * signal: presenting an already-rotated token revokes the entire token family
 * and signs the user out everywhere. That is exactly the right behaviour
 * against a stolen token — and it makes concurrent refreshes actively
 * dangerous. Three requests hitting 401 at once would each post the same
 * cookie; the first rotates it, the other two are indistinguishable from an
 * attacker replaying it, and the user is logged out of their own session for
 * doing nothing but loading a page with three widgets on it.
 *
 * So refresh happens at most once at a time, and everyone waits on the same
 * promise.
 */
let inFlightRefresh: Promise<boolean> | null = null;

export function refresh(): Promise<boolean> {
  if (!inFlightRefresh) {
    inFlightRefresh = fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    })
      .then((r) => {
        captureCsrf(r);
        return r.ok;
      })
      .catch(() => false)
      .finally(() => {
        inFlightRefresh = null;
      });
  }
  return inFlightRefresh;
}

/** Called when the session is gone for good, so the app can route to /login. */
type SessionEndedHandler = () => void;
let onSessionEnded: SessionEndedHandler = () => {};

export function setSessionEndedHandler(handler: SessionEndedHandler): void {
  onSessionEnded = handler;
}

export interface RequestOptions {
  method?: string;
  /** Sent as JSON. */
  body?: unknown;
  /** Sent as application/x-www-form-urlencoded (the login endpoint wants this). */
  form?: Record<string, string>;
  /** Skip the refresh-and-retry dance. */
  noRetry?: boolean;
  signal?: AbortSignal;
}

async function toError(response: Response): Promise<ApiError> {
  let detail = response.statusText || "Request failed";
  let code: string | undefined;

  try {
    const data = await response.json();
    const raw = data?.detail ?? data?.message;
    if (typeof raw === "string") {
      detail = raw;
    } else if (Array.isArray(raw)) {
      // FastAPI validation errors arrive as a list of {loc, msg}.
      detail = raw.map((e: { msg?: string }) => e?.msg ?? "Invalid value").join(". ");
    } else if (raw && typeof raw === "object" && typeof raw.reason === "string") {
      detail = raw.reason;
    }
    if (typeof data?.detail === "string") code = data.detail;
  } catch {
    /* a non-JSON error body is fine; the status still tells us what happened */
  }

  // The API answers register/login failures with a stable machine code rather
  // than prose. Translate the ones a user can act on.
  const friendly: Record<string, string> = {
    LOGIN_BAD_CREDENTIALS: "That email and password do not match an account.",
    LOGIN_USER_NOT_VERIFIED: "Please confirm your email address first.",
    REGISTER_USER_ALREADY_EXISTS: "An account with that email already exists.",
    RESET_PASSWORD_BAD_TOKEN: "That reset link has expired or was already used.",
    VERIFY_USER_BAD_TOKEN: "That confirmation link has expired or was already used.",
    VERIFY_USER_ALREADY_VERIFIED: "That address is already confirmed. You can sign in.",
  };
  if (code && friendly[code]) detail = friendly[code];

  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfter = retryAfterHeader ? Number(retryAfterHeader) : undefined;
  if (response.status === 429) {
    detail = retryAfter
      ? `Too many attempts. Try again in ${retryAfter} seconds.`
      : "Too many attempts. Please wait a moment and try again.";
  }

  return new ApiError(response.status, detail, code, retryAfter);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers();
  let body: BodyInit | undefined;

  if (options.form) {
    headers.set("Content-Type", "application/x-www-form-urlencoded");
    body = new URLSearchParams(options.form).toString();
  } else if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }

  if (!SAFE_METHODS.has(method)) {
    const token = await ensureCsrf();
    if (token) headers.set(CSRF_HEADER, token);
  }

  const send = () =>
    fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body,
      credentials: "include",
      signal: options.signal,
    });

  let response = await send();
  captureCsrf(response);

  // An expired access token is the ordinary case, not an error: refresh once
  // and replay. Only for endpoints where a session could plausibly exist.
  const retryable =
    response.status === 401 && !options.noRetry && !NO_REFRESH_RETRY.includes(path);

  if (retryable) {
    if (await refresh()) {
      response = await send();
      captureCsrf(response);
    } else {
      onSessionEnded();
    }
  }

  if (response.status === 401 && !NO_REFRESH_RETRY.includes(path)) onSessionEnded();

  if (!response.ok) {
    // 401 from /auth/login with a step-up header is not a failure — the
    // password was right and the second factor is now due.
    if (response.status === 401) {
      const stepUp = response.headers.get("X-Step-Up-Token");
      if (stepUp) {
        const methods = (response.headers.get("X-Step-Up-Methods") ?? "totp").split(",");
        throw new StepUpRequired(stepUp, methods);
      }
    }
    throw await toError(response);
  }

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, o?: RequestOptions) => request<T>(path, { ...o, method: "GET" }),
  post: <T>(path: string, body?: unknown, o?: RequestOptions) =>
    request<T>(path, { ...o, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, o?: RequestOptions) =>
    request<T>(path, { ...o, method: "PATCH", body }),
  del: <T>(path: string, o?: RequestOptions) => request<T>(path, { ...o, method: "DELETE" }),
};
