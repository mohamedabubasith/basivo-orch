/**
 * The workspace the app is currently looking at.
 *
 * Every org-scoped URL needs an id, so before this existed each page fetched
 * `/orgs` itself and picked `[0]`. That is three requests for one fact, and
 * three chances for two pages to disagree about which workspace they are
 * showing. The shell resolves it once and everything below reads it.
 *
 * The choice is remembered, because a workspace switcher that resets on every
 * reload is not a switcher.
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

import { api } from "./api";

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  role: string;
  permissions: string[];
}

interface WorkspaceState {
  workspaces: Workspace[];
  current: Workspace | null;
  /** Convenience: the id every org-scoped request needs. */
  orgId: string | null;
  loading: boolean;
  error: string | null;
  select: (id: string) => void;
  refresh: () => Promise<Workspace[]>;
}

const WorkspaceContext = createContext<WorkspaceState | null>(null);
const STORAGE_KEY = "basivo.workspace";

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<Workspace[]> => {
    try {
      const list = await api.get<Workspace[]>("/orgs");
      setWorkspaces(list);
      setError(null);
      return list;
    } catch {
      setError("Could not load your workspaces.");
      return [];
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const select = useCallback((id: string) => {
    setSelectedId(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Private browsing can refuse writes. Losing the preference is fine;
      // throwing here would take the whole shell down with it.
    }
  }, []);

  // Resolve against the list rather than trusting the stored id: membership
  // can be revoked between visits, and a remembered id would otherwise send
  // every request to a workspace the user can no longer see.
  const current = useMemo(() => {
    if (workspaces.length === 0) return null;
    const remembered = selectedId ?? readStored();
    return workspaces.find((w) => w.id === remembered) ?? workspaces[0];
  }, [workspaces, selectedId]);

  const value = useMemo<WorkspaceState>(
    () => ({
      workspaces,
      current,
      orgId: current?.id ?? null,
      loading,
      error,
      select,
      refresh,
    }),
    [workspaces, current, loading, error, select, refresh],
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function useWorkspace(): WorkspaceState {
  const context = useContext(WorkspaceContext);
  if (!context)
    throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return context;
}
