import { type FormEvent, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ApiError, StepUpRequired } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";
import { SsoButtons } from "./SsoButtons";

export function Login() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Where the user was headed before being bounced to sign in.
  const from = (location.state as { from?: string } | null)?.from ?? "/app";
  const notice = (location.state as { notice?: string } | null)?.notice;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setBusy(true);
    try {
      await signIn(email, password);
      navigate(from, { replace: true });
    } catch (err) {
      if (err instanceof StepUpRequired) {
        // The password was right; the second factor is outstanding. The step-up
        // token is a credential, so it travels in router state (memory) and is
        // never written to storage or put in the URL.
        navigate("/two-factor", {
          replace: true,
          state: { stepUpToken: err.token, methods: err.methods, from },
        });
        return;
      }
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not sign in. Please try again.",
      );
      setBusy(false);
      return;
    }
    // Left busy on the success path: the redirect is about to unmount this
    // form, and clearing it first makes the button flicker back to its idle
    // state for a frame.
  }

  return (
    <AuthLayout
      title="Welcome back"
      subtitle="Pick up where your pipelines left off."
      footer={
        <>
          New here?{" "}
          <Link
            to="/register"
            className="font-medium text-brand-300 hover:text-brand-400"
          >
            Create an account
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {notice && <Alert tone="success">{notice}</Alert>}
        {error && <Alert>{error}</Alert>}

        <Field
          label="Email"
          type="email"
          name="email"
          autoComplete="username"
          required
          autoFocus
          disabled={busy}
          placeholder="you@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <div>
          <Field
            label="Password"
            type="password"
            name="password"
            autoComplete="current-password"
            revealable
            required
            disabled={busy}
            placeholder="••••••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className="mt-2 text-right">
            <Link
              to="/forgot-password"
              className="text-sm text-ink-400 transition-colors hover:text-ink-200"
            >
              Forgot password?
            </Link>
          </div>
        </div>

        <Button type="submit" full size="lg" loading={busy}>
          Sign in
        </Button>
      </form>

      <SsoButtons />
    </AuthLayout>
  );
}
