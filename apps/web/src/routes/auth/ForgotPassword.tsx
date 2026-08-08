import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.post("/auth/forgot-password", { email });
      setSent(true);
    } catch (err) {
      // The API answers identically for known and unknown addresses, so an
      // error here is a transport or rate-limit problem — never "no such user".
      // Surfacing anything address-specific would undo that on the client.
      setError(
        err instanceof ApiError ? err.message : "Could not send the email. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <AuthLayout
        title="Check your email"
        subtitle={
          <>
            If an account exists for <span className="text-ink-200">{email}</span>, a reset link
            is on its way.
          </>
        }
        footer={
          <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
            Back to sign in
          </Link>
        }
      >
        <p className="text-[0.95rem] leading-relaxed text-ink-400">
          The link works once and expires in an hour. If it does not arrive, check your spam
          folder before trying again.
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Reset your password"
      subtitle="We will email you a link to set a new one."
      footer={
        <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
          Back to sign in
        </Link>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}

        <Field
          label="Email"
          type="email"
          name="email"
          autoComplete="username"
          required
          autoFocus
          placeholder="you@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <Button type="submit" full size="lg" loading={busy}>
          Send reset link
        </Button>
      </form>
    </AuthLayout>
  );
}
