import { motion } from "motion/react";
import { type FormEvent, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";
import { SsoButtons } from "./SsoButtons";

/**
 * Local password feedback, shown while typing.
 *
 * A hint, not a gate. The API runs the real policy — length, a breach check
 * against Have I Been Pwned, similarity to the email address — and its answer
 * is the one that counts. Duplicating the whole ruleset here would create two
 * policies to keep in step, and a client-side check is not a security control.
 */
function strengthOf(password: string): {
  score: number;
  label: string;
  tone: string;
} {
  if (!password) return { score: 0, label: "", tone: "" };
  if (password.length < 12) {
    return {
      score: 1,
      label: "Too short. 12 characters minimum",
      tone: "bg-err-500",
    };
  }

  let score = 1;
  if (password.length >= 16) score += 1;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
  if (/\d/.test(password)) score += 1;
  if (/[^\w\s]/.test(password)) score += 1;

  const labels: Record<number, { label: string; tone: string }> = {
    1: { label: "Weak", tone: "bg-err-500" },
    2: { label: "Fair", tone: "bg-warn-500" },
    3: { label: "Good", tone: "bg-warn-500" },
    4: { label: "Strong", tone: "bg-ok-500" },
    5: { label: "Very strong", tone: "bg-ok-500" },
  };
  return { score, ...labels[score] };
}

export function Register() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const strength = useMemo(() => strengthOf(password), [password]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.post("/auth/register", { email, password });
      setDone(true);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not create the account. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthLayout
        title="Account created"
        subtitle={
          <>
            We sent a confirmation link to{" "}
            <span className="text-ink-200">{email}</span>.
          </>
        }
      >
        <div className="space-y-5">
          <Alert tone="success">
            Your account is ready. You can sign in now.
          </Alert>

          <p className="text-[0.95rem] leading-relaxed text-ink-400">
            Confirming your address unlocks email notifications and password
            recovery. It is not required to sign in, so you do not have to wait
            for the message to arrive.
          </p>

          {/* Deliberately not a dead end. Email delivery depends on an external
              service; if it is misconfigured or slow, a screen that only says
              "check your email" leaves a working account unreachable behind a
              message that may never come. */}
          <Button full size="lg" onClick={() => navigate("/login")}>
            Continue to sign in
          </Button>

          <p className="text-center text-sm text-ink-500">
            Nothing arrived? Check spam, or{" "}
            <Link
              to="/forgot-password"
              className="text-brand-300 hover:text-brand-400"
            >
              request a new link
            </Link>
            .
          </p>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Free while we are in beta."
      footer={
        <>
          Already have one?{" "}
          <Link
            to="/login"
            className="font-medium text-brand-300 hover:text-brand-400"
          >
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}

        <Field
          label="Work email"
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
            autoComplete="new-password"
            revealable
            required
            disabled={busy}
            placeholder="At least 12 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />

          {password && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              className="mt-2.5 flex items-center gap-3"
            >
              <div className="flex h-1 flex-1 gap-1" aria-hidden="true">
                {[1, 2, 3, 4, 5].map((i) => (
                  <motion.span
                    key={i}
                    animate={{ opacity: i <= strength.score ? 1 : 0.25 }}
                    className={`h-full flex-1 rounded-full transition-colors duration-300 ${
                      i <= strength.score ? strength.tone : "bg-ink-700"
                    }`}
                  />
                ))}
              </div>
              <span className="w-48 flex-none text-right text-xs text-ink-400">
                {strength.label}
              </span>
            </motion.div>
          )}
        </div>

        <Button type="submit" full size="lg" loading={busy}>
          Create account
        </Button>

        <p className="text-center text-xs leading-relaxed text-ink-500">
          By creating an account you agree to our terms and privacy policy.
        </p>
      </form>

      <SsoButtons />
    </AuthLayout>
  );
}
