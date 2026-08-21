import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";

interface StepUpState {
  stepUpToken?: string;
  methods?: string[];
  from?: string;
}

export function TwoFactor() {
  const { completeTwoFactor } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const state = (location.state ?? {}) as StepUpState;

  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Reaching this screen without a step-up token means a reload or a direct
  // visit. The token only lives in router state, so the sign-in has to restart.
  if (!state.stepUpToken) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ notice: "Please sign in again." }}
      />
    );
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await completeTwoFactor(state.stepUpToken!, code.trim());
      navigate(state.from ?? "/app", { replace: true });
    } catch (err) {
      // The step-up token is valid for five minutes. Once it lapses the only
      // way forward is a fresh sign-in, so say that rather than leaving the
      // user retrying codes against a dead token.
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", {
          replace: true,
          state: { notice: "That sign-in attempt expired. Please try again." },
        });
        return;
      }
      setError(
        err instanceof ApiError ? err.message : "That code was not accepted.",
      );
      setCode("");
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Two-factor authentication"
      subtitle="Enter the 6-digit code from your authenticator app."
      footer={
        <Link
          to="/login"
          className="font-medium text-brand-300 hover:text-brand-400"
        >
          Cancel and sign in again
        </Link>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}

        <Field
          ref={inputRef}
          label="Authentication code"
          name="code"
          // `one-time-code` is what lets iOS and Android offer the code from
          // the keyboard instead of making people switch apps.
          autoComplete="one-time-code"
          inputMode="numeric"
          required
          placeholder="000000"
          maxLength={12}
          className="text-center font-mono text-lg tracking-[0.4em]"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          hint="Lost your device? Use one of your recovery codes instead."
        />

        <Button
          type="submit"
          full
          size="lg"
          loading={busy}
          disabled={code.trim().length < 6}
        >
          Verify
        </Button>
      </form>
    </AuthLayout>
  );
}
