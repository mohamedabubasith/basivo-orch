/**
 * Session state for the app.
 *
 * The source of truth is the API, never this module: `useAuth().user` is a
 * cache of `GET /users/me`, and every protected route re-checks on mount. The
 * browser cannot inspect the HttpOnly session cookie, so "am I signed in?" is
 * only ever answerable by asking the server — which is also the answer that
 * stays correct when a session is revoked from another device.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { api, setSessionEndedHandler } from "./api";

export interface User {
  id: string;
  email: string;
  is_active: boolean;
  is_verified: boolean;
  is_superuser: boolean;
  totp_enabled?: boolean;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
}

interface AuthState {
  user: User | null;
  /** True until the first `GET /users/me` settles, so routes can hold render. */
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  completeTwoFactor: (stepUpToken: string, code: string) => Promise<void>;
  signOut: () => Promise<void>;
  reload: () => Promise<User | null>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  // Guards against a state update after unmount during the initial probe.
  const mounted = useRef(true);

  const reload = useCallback(async (): Promise<User | null> => {
    try {
      const me = await api.get<User>("/users/me");
      if (mounted.current) setUser(me);
      return me;
    } catch {
      if (mounted.current) setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    // One probe at startup. A 401 here is the normal anonymous case, not an
    // error worth surfacing: the landing page is public.
    reload().finally(() => {
      if (mounted.current) setLoading(false);
    });
    return () => {
      mounted.current = false;
    };
  }, [reload]);

  useEffect(() => {
    // The client calls this when a refresh fails, i.e. the session is
    // genuinely over — including the case where reuse detection revoked the
    // whole family. Drop local state so protected routes redirect.
    setSessionEndedHandler(() => setUser(null));
  }, []);

  const signIn = useCallback(
    async (email: string, password: string) => {
      // OAuth2 password form: the field is `username` even though it holds an
      // email address. Throws StepUpRequired when 2FA is enabled.
      await api.post("/auth/login", undefined, {
        form: { username: email, password },
      });
      await reload();
    },
    [reload],
  );

  const completeTwoFactor = useCallback(
    async (stepUpToken: string, code: string) => {
      await api.post("/auth/2fa/verify", { step_up_token: stepUpToken, code });
      await reload();
    },
    [reload],
  );

  const signOut = useCallback(async () => {
    try {
      // Revokes the refresh token server-side. Clearing cookies alone would
      // leave a captured token usable.
      await api.post("/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, signIn, completeTwoFactor, signOut, reload }),
    [user, loading, signIn, completeTwoFactor, signOut, reload],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>.");
  return context;
}
