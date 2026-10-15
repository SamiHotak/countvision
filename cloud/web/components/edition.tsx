"use client";

import * as React from "react";

import { api } from "@/lib/api";
import { CLOUD_DEFAULT, type Edition } from "@/lib/edition";

interface EditionState {
  edition: Edition;
  loaded: boolean;
  refresh: () => Promise<Edition>;
}

const EditionContext = React.createContext<EditionState>({
  edition: CLOUD_DEFAULT, loaded: false, refresh: async () => CLOUD_DEFAULT,
});

/** Loads /api/edition once. Pages hide cloud-only parts (billing, Google, Telegram) in the local edition. */
export function EditionProvider({ children }: { children: React.ReactNode }) {
  const [edition, setEdition] = React.useState<Edition>(CLOUD_DEFAULT);
  const [loaded, setLoaded] = React.useState(false);
  const refresh = React.useCallback(async () => {
    try {
      const e = await api.get<Edition>("/api/edition");
      setEdition(e);
      return e;
    } catch {
      return CLOUD_DEFAULT; // an older API without /api/edition: behave like the cloud
    } finally {
      setLoaded(true);
    }
  }, []);
  React.useEffect(() => {
    void refresh();
  }, [refresh]);
  const value = React.useMemo(() => ({ edition, loaded, refresh }), [edition, loaded, refresh]);
  return <EditionContext.Provider value={value}>{children}</EditionContext.Provider>;
}

export function useEdition(): EditionState {
  return React.useContext(EditionContext);
}

export const useIsLocal = () => useEdition().edition.edition === "local";
