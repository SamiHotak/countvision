"use client";

import * as React from "react";

import { api, ApiError, errorText, type Org } from "@/lib/api";

/** Load one organization (and remember it as the last used one). */
export function useOrg(orgId: string) {
  const [org, setOrg] = React.useState<Org | null>(null);
  const [error, setError] = React.useState<{ notFound: boolean; text: string } | null>(null);

  const reload = React.useCallback(async () => {
    try {
      const data = await api.get<Org>(`/api/orgs/${orgId}`);
      setOrg(data);
      setError(null);
      try {
        localStorage.setItem("cv:last-org", orgId);
      } catch {
        /* storage blocked: ignore */
      }
    } catch (e) {
      setError({ notFound: e instanceof ApiError && (e.status === 404 || e.status === 422), text: errorText(e) });
    }
  }, [orgId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  return { org, error, reload };
}
