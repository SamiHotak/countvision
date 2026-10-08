"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { GoogleButton } from "@/components/google-button";
import { SignupForm } from "@/components/signup-form";
import { safeNext } from "@/lib/utils";

function SignupPage() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  return (
    <AuthShell
      title="Create your account"
      intro={<>Already have one? <Link href={`/login?next=${encodeURIComponent(next)}`} className="text-accent">Log in</Link></>}
    >
      <GoogleButton next={next} />
      <SignupForm next={next} />
    </AuthShell>
  );
}

export default function Page() {
  return (
    <React.Suspense>
      <SignupPage />
    </React.Suspense>
  );
}
