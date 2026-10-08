"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fieldErrors, fmtDate, type Me, type Ok } from "@/lib/api";

type Msg = { tone: "success" | "error"; text: string } | null;

function Profile({ me }: { me: Me }) {
  const { setMe } = useMe();
  const [msg, setMsg] = React.useState<Msg>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      setMe(await api.patch<Me>("/api/me", { name: new FormData(e.currentTarget).get("name") }));
      setMsg({ tone: "success", text: "Name saved." });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4 p-5" noValidate>
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      <div className="grid max-w-[640px] gap-4 sm:grid-cols-2">
        <Field label="Name" name="name" defaultValue={me.name} required maxLength={120} autoComplete="name" />
        <Field label="Email" name="email" value={me.email} readOnly
          hint={me.email_verified ? "Confirmed" : "Not confirmed yet"} />
      </div>
      <Button type="submit" variant="secondary" busy={busy}>Save name</Button>
    </form>
  );
}

function Password({ me }: { me: Me }) {
  const [msg, setMsg] = React.useState<Msg>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const formRef = React.useRef<HTMLFormElement>(null);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setMsg(null);
    setFields({});
    if (form.get("new_password") !== form.get("repeat")) {
      setFields({ repeat: "The two passwords are different." });
      return;
    }
    setBusy(true);
    try {
      const res = await api.post<Ok>("/api/me/password", {
        current_password: me.has_password ? form.get("current_password") : null,
        new_password: form.get("new_password"),
      });
      formRef.current?.reset();
      setMsg({ tone: "success", text: res.message ?? "Password saved." });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err) });
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form ref={formRef} onSubmit={submit} className="space-y-4 p-5" noValidate>
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      {!me.has_password ? (
        <p className="text-muted">You log in with Google. You can also set a password to log in with your email.</p>
      ) : null}
      <div className="grid max-w-[640px] gap-4 sm:grid-cols-2">
        {me.has_password ? (
          <div className="sm:col-span-2 sm:max-w-[312px]">
            <Field label="Current password" name="current_password" type="password" autoComplete="current-password" error={fields.current_password} />
          </div>
        ) : null}
        <Field label="New password" name="new_password" type="password" autoComplete="new-password" minLength={10}
          error={fields.password} hint="At least 10 characters." />
        <Field label="Repeat new password" name="repeat" type="password" autoComplete="new-password" error={fields.repeat} />
      </div>
      <Button type="submit" variant="secondary" busy={busy}>{me.has_password ? "Change password" : "Set password"}</Button>
    </form>
  );
}

function Sessions() {
  const [msg, setMsg] = React.useState<Msg>(null);
  const [busy, setBusy] = React.useState(false);
  async function logoutOthers() {
    setBusy(true);
    try {
      const res = await api.post<Ok>("/api/auth/logout-all");
      setMsg({ tone: "success", text: res.message ?? "Done." });
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4 p-5">
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      <div className="flex flex-wrap items-center gap-4">
        <p className="max-w-[56ch] flex-1 text-muted">Lost a phone or used a shared computer? Log out everywhere except here.</p>
        <Button variant="secondary" onClick={logoutOthers} busy={busy}>Log out other devices</Button>
      </div>
    </div>
  );
}

function DeleteAccount({ me }: { me: Me }) {
  const router = useRouter();
  const { setMe } = useMe();
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/me/delete", { password: me.has_password ? new FormData(e.currentTarget).get("password") : null });
      setMe(null);
      router.replace("/login");
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-4 p-5">
      <p className="max-w-[56ch] flex-1 text-muted">
        Deletes your account. Organizations where you are the only member are deleted too.
        Member since {fmtDate(me.created_at)}.
      </p>
      <Button variant="danger" onClick={() => { setError(null); setOpen(true); }}>Delete account</Button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Delete your account?">
        <form onSubmit={submit} className="space-y-4">
          {error ? <Notice tone="error">{error}</Notice> : null}
          <p className="text-muted">This cannot be undone.</p>
          {me.has_password ? (
            <Field label="Your password" name="password" type="password" autoComplete="current-password" autoFocus />
          ) : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Keep my account</Button>
            <Button type="submit" variant="danger" busy={busy}>Delete forever</Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}

export default function AccountPage() {
  const { me } = useMe();
  if (!me) return null;
  return (
    <>
      <PageTitle title="Your account" />
      <div className="space-y-6">
        <Panel><PanelHead title="Profile" /><Profile me={me} /></Panel>
        <Panel><PanelHead title="Password" /><Password me={me} /></Panel>
        <Panel><PanelHead title="Logged-in devices" /><Sessions /></Panel>
        <Panel className="border-danger/40"><PanelHead title="Delete account" /><DeleteAccount me={me} /></Panel>
      </div>
    </>
  );
}
