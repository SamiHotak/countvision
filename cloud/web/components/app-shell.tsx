"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import * as React from "react";

import { Wordmark } from "@/components/brand";
import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/panel";
import { api, errorText, type Me, type Ok } from "@/lib/api";
import { cn } from "@/lib/utils";

function NavLink({ href, children, exact = false }: { href: string; children: React.ReactNode; exact?: boolean }) {
  const path = usePathname();
  const active = exact ? path === href : path === href || path.startsWith(href + "/");
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex h-9 items-center rounded-[var(--radius-s)] px-3 no-underline",
        active ? "bg-raise text-text" : "text-muted hover:bg-raise/60 hover:text-text",
      )}
    >
      {active ? <span className="-ml-3 mr-[9px] h-4 w-[3px] rounded-r bg-accent" aria-hidden /> : null}
      {children}
    </Link>
  );
}

function OrgSwitcher({ me, orgId }: { me: Me; orgId: string | null }) {
  const router = useRouter();
  return (
    <label className="block">
      <span className="sr-only">Organization</span>
      <select
        value={orgId ?? ""}
        onChange={(e) => router.push(e.target.value === "__new" ? "/app/new-org" : `/app/orgs/${e.target.value}`)}
        className="h-10 w-full rounded-[var(--radius-s)] border border-rule bg-ink px-2.5 font-medium text-text"
        data-testid="org-switcher"
      >
        {orgId === null ? <option value="">Choose organization</option> : null}
        {me.organizations.map((o) => (
          <option key={o.id} value={o.id}>{o.name}</option>
        ))}
        <option value="__new">+ New organization</option>
      </select>
    </label>
  );
}

function VerifyBanner() {
  const [msg, setMsg] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [busy, setBusy] = React.useState(false);
  async function resend() {
    setBusy(true);
    try {
      const res = await api.post<Ok>("/api/auth/resend-verification");
      setMsg({ tone: "success", text: res.message ?? "Sent." });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Notice tone={msg?.tone ?? "warn"} className="mb-6 flex flex-wrap items-center gap-x-4 gap-y-2">
      <span className="mr-auto">{msg ? msg.text : "Please confirm your email address. We sent you a link."}</span>
      {!msg ? <Button size="sm" variant="secondary" onClick={resend} busy={busy}>Send the link again</Button> : null}
    </Notice>
  );
}

export function AppShell({ me, orgId, children }: { me: Me; orgId: string | null; children: React.ReactNode }) {
  const router = useRouter();
  const { setMe } = useMe();
  const [open, setOpen] = React.useState(false);
  const path = usePathname();
  React.useEffect(() => setOpen(false), [path]);

  async function logout() {
    await api.post("/api/auth/logout").catch(() => undefined);
    setMe(null);
    router.replace("/login");
  }

  const nav = (
    <nav className="flex flex-col gap-1" aria-label="Main">
      {orgId ? (
        <>
          <NavLink href={`/app/orgs/${orgId}`} exact>Overview</NavLink>
          <NavLink href={`/app/orgs/${orgId}/members`}>Members</NavLink>
          <NavLink href={`/app/orgs/${orgId}/settings`}>Settings</NavLink>
        </>
      ) : null}
    </nav>
  );

  const footer = (
    <div className="mt-auto space-y-1 border-t border-rule pt-4">
      <NavLink href="/app/account">
        <span className="min-w-0 truncate">{me.name}</span>
      </NavLink>
      <button onClick={logout} className="flex h-9 w-full cursor-pointer items-center rounded-[var(--radius-s)] px-3 text-muted hover:bg-raise/60 hover:text-text">
        Log out
      </button>
    </div>
  );

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[248px_minmax(0,1fr)]">
      {/* desktop sidebar */}
      <aside className="sticky top-0 hidden h-dvh flex-col gap-6 border-r border-rule bg-panel px-4 py-5 lg:flex">
        <Wordmark />
        <OrgSwitcher me={me} orgId={orgId} />
        {nav}
        {footer}
      </aside>

      {/* phone top bar */}
      <header className="sticky top-0 z-10 flex items-center gap-3 border-b border-rule bg-ink/95 px-4 py-3 backdrop-blur lg:hidden">
        <Wordmark />
        <button
          className="ml-auto h-9 cursor-pointer rounded-[var(--radius-s)] border border-rule px-3 text-sm"
          aria-expanded={open}
          aria-controls="mobile-nav"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Close" : "Menu"}
        </button>
      </header>
      {open ? (
        <div id="mobile-nav" className="space-y-5 border-b border-rule bg-panel px-4 py-4 lg:hidden">
          <OrgSwitcher me={me} orgId={orgId} />
          {nav}
          {footer}
        </div>
      ) : null}

      <main className="mx-auto w-full max-w-[1040px] px-4 py-6 sm:px-8 sm:py-9">
        {!me.email_verified ? <VerifyBanner /> : null}
        {children}
      </main>
    </div>
  );
}

export function PageTitle({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="mb-7 flex flex-wrap items-end gap-4">
      <h1 className="mr-auto text-2xl font-semibold leading-tight">{title}</h1>
      {children}
    </div>
  );
}
