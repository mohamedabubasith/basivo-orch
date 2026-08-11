/**
 * Light / dark, with a third state that is not a colour: "system".
 *
 * Two states would be a bug disguised as simplicity. Someone whose OS switches
 * to light at sunrise wants the app to follow; storing only "dark" or "light"
 * freezes them at whatever they happened to pick once. So the stored value is
 * the *preference* — including "follow the system" — and the resolved colour is
 * derived from it.
 *
 * The initial value is applied in index.html before React mounts. Doing it here
 * would paint a dark screen first and correct it a frame later, which is the
 * flash every themed app is judged by.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "basivo.theme";

interface ThemeState {
  choice: ThemeChoice;
  /** What the choice currently resolves to. */
  resolved: "light" | "dark";
  setChoice: (choice: ThemeChoice) => void;
}

const ThemeContext = createContext<ThemeState | null>(null);

function systemTheme(): "light" | "dark" {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function readChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // Private browsing can refuse reads; the default is fine.
  }
  return "system";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice);
  const [system, setSystem] = useState<"light" | "dark">(() =>
    typeof window === "undefined" ? "dark" : systemTheme(),
  );

  // Follow the OS while the choice is "system" — including a change made while
  // the tab is open, which is exactly when sunrise happens.
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: light)");
    const update = () => setSystem(query.matches ? "light" : "dark");
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  const resolved = choice === "system" ? system : choice;

  useEffect(() => {
    // `system` deliberately clears the attribute rather than writing a value,
    // so the CSS media query — not JavaScript — decides.
    const root = document.documentElement;
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    root.style.colorScheme = resolved;
  }, [choice, resolved]);

  const setChoice = useCallback((next: ThemeChoice) => {
    setChoiceState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Preference only.
    }
  }, []);

  const value = useMemo(() => ({ choice, resolved, setChoice }), [choice, resolved, setChoice]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside <ThemeProvider>");
  return context;
}
