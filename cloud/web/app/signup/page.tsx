"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { useEdition } from "@/components/edition";
import { GoogleButton } from "@/components/google-button";
import { SignupForm } from "@/components/signup-form";
import { safeNext } from "@/lib/utils";

function SignupPage() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const router = useRouter();
  const { edition, loaded } = useEdition();
  React.useEffect(() => {
    // CountVision Local has no public sign-up (first start = the setup wizard, then invitations)
    if (loaded && !edition.signup) router.replace(edition.needs_setup ? "/setup" : "/login");
  }, [loaded, edition, router]);
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
