"use client";

import { usePathname, useRouter } from "next/navigation";
import * as React from "react";

import { api, ApiError, type Me } from "@/lib/api";

interface MeState {
  me: Me | null;
  loading: boolean;
  refresh: () => Promise<Me | null>;
  setMe: (me: Me | null) => void;
}

const MeContext = React.createContext<MeState | null>(null);

/** Loads the logged-in user once; pages call refresh() after changes. */
export function MeProvider({ children }: { children: React.ReactNode }) {
  const [me, setMe] = React.useState<Me | null>(null);
  const [loading, setLoading] = React.useState(true);

  const refresh = React.useCallback(async () => {
    try {
      const data = await api.get<Me>("/api/me");
      setMe(data);
      return data;
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) console.warn("Could not load the account", e);
      setMe(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = React.useMemo(() => ({ me, loading, refresh, setMe }), [me, loading, refresh]);
  return <MeContext.Provider value={value}>{children}</MeContext.Provider>;
}

export function useMe(): MeState {
  const ctx = React.useContext(MeContext);
  if (!ctx) throw new Error("useMe outside MeProvider");
  return ctx;
}

/** Shows children only for logged-in users; others go to /login?next=<this page>. */
export function RequireLogin({ children }: { children: (me: Me) => React.ReactNode }) {
  const { me, loading } = useMe();
  const router = useRouter();
  const path = usePathname();
  React.useEffect(() => {
    if (!loading && !me) router.replace(`/login?next=${encodeURIComponent(path)}`);
  }, [loading, me, path, router]);
  if (loading || !me) return <Loading />;
  return <>{children(me)}</>;
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex min-h-[40vh] items-center justify-center gap-3 text-muted" role="status">
      <span className="size-4 animate-spin rounded-full border-2 border-accent border-r-transparent" />
      {label}
    </div>
  );
}
