import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";

export function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token");

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mismatch) return;
    setError(null);
    setBusy(true);
    try {
      await api.post("/auth/reset-password", { token, new_password: password });
      // Deliberately not signed in here. Completing a reset proves control of
      // the inbox, not of the password — making the user type the new one is
      // what confirms they know it, and it keeps a stolen reset link from
      // handing over a live session in one click.
      navigate("/login", {
        replace: true,
        state: { notice: "Password updated. Sign in with your new password." },
      });
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not reset the password. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <AuthLayout
        title="That link is incomplete"
        footer={
          <Link
            to="/forgot-password"
            className="font-medium text-brand-300 hover:text-brand-400"
          >
            Request a new link
          </Link>
        }
      >
        <Alert>
          This reset link is missing its token. Please request a new one.
        </Alert>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Choose a new password"
      subtitle="This also signs out every other device."
      footer={
        <Link
          to="/login"
          className="font-medium text-brand-300 hover:text-brand-400"
        >
          Back to sign in
        </Link>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}

        <Field
          label="New password"
          type="password"
          name="new-password"
          autoComplete="new-password"
          required
          autoFocus
          placeholder="At least 12 characters"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <Field
          label="Confirm new password"
          type="password"
          name="confirm-password"
          autoComplete="new-password"
          required
          placeholder="Type it again"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          error={mismatch ? "Those passwords do not match." : null}
        />

        <Button
          type="submit"
          full
          size="lg"
          loading={busy}
          disabled={mismatch || !password}
        >
          Update password
        </Button>
      </form>
    </AuthLayout>
  );
}
