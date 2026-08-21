/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Where the API is. Empty in production: the app calls relative URLs. */
  readonly VITE_API_URL?: string;
  /**
   * The console's origin, when the landing page is served from a second
   * hostname. Baked in at build time because a session cookie belongs to one
   * origin, so the app must have exactly one home.
   */
  readonly VITE_CONSOLE_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
