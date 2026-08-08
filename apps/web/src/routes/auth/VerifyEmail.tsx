import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { Alert, Spinner } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";

type State = "working" | "ok" | "failed";

export function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const [state, setState] = useState<State>("working");
  const [message, setMessage] = useState("");
  // React 18+ mounts effects twice in development. Verification tokens are
  // single-use, so a second call would report "already used" on a link that
  // just worked.
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;

    if (!token) {
      setState("failed");
      setMessage("That link is missing its confirmation token.");
      return;
    }

    api
      .post("/auth/verify", { token })
      .then(() => setState("ok"))
      .catch((err: unknown) => {
        setState("failed");
        setMessage(
          err instanceof ApiError
            ? err.message
            : "We could not confirm that link. It may have expired.",
        );
      });
  }, [token]);

  if (state === "working") {
    return (
      <AuthLayout title="Confirming your email">
        <div className="flex items-center justify-center gap-3 py-6 text-ink-400">
          <Spinner className="h-5 w-5" />
          <span>One moment…</span>
        </div>
      </AuthLayout>
    );
  }

  if (state === "ok") {
    return (
      <AuthLayout
        title="Email confirmed"
        subtitle="Your account is active."
        footer={
          <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
            Continue to sign in
          </Link>
        }
      >
        <Alert tone="success">You can now sign in and build your first pipeline.</Alert>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="That link did not work"
      footer={
        <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
          Back to sign in
        </Link>
      }
    >
      <div className="space-y-4">
        <Alert>{message}</Alert>
        <p className="text-[0.95rem] text-ink-400">
          Confirmation links expire and can only be used once. Register again with the same
          address to get a fresh one.
        </p>
      </div>
    </AuthLayout>
  );
}
