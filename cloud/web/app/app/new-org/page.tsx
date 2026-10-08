"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/input";
import { Notice, Panel } from "@/components/ui/panel";
import { api, errorText, fieldErrors, type Org } from "@/lib/api";

export default function NewOrgPage() {
  const router = useRouter();
  const { me, refresh } = useMe();
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const first = (me?.organizations.length ?? 0) === 0;

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFields({});
    try {
      const org = await api.post<Org>("/api/orgs", { name: new FormData(e.currentTarget).get("name") });
      await refresh();
      router.replace(`/app/orgs/${org.id}`);
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
      setBusy(false);
    }
  }

  return (
    <>
      <PageTitle title={first ? "Name your business" : "New organization"} />
      <Panel className="max-w-[520px] p-5 sm:p-6">
        <p className="mb-5 text-muted">
          {first
            ? "An organization holds your sites, cameras and team. Use the name your staff knows, for example the shop or company name."
            : "Use a separate organization for a separate business or customer. Members, sites and cameras are not shared between organizations."}
        </p>
        <form onSubmit={submit} className="space-y-4" noValidate>
          {error && !fields.name ? <Notice tone="error">{error}</Notice> : null}
          <Field label="Organization name" name="name" required minLength={2} maxLength={120} autoFocus
            placeholder="Café Sonne" error={fields.name} />
          <Button type="submit" busy={busy}>Create organization</Button>
        </form>
      </Panel>
    </>
  );
}
