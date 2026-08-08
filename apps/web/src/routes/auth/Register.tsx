import { type FormEvent, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { Alert, Button, Field } from "../../components/ui";
import { AuthLayout } from "./AuthLayout";
import { SsoButtons } from "./SsoButtons";

/**
 * Local password feedback, shown while typing.
 *
 * This is a hint, not a gate. The API runs the real policy — length, breach
 * check against Have I Been Pwned, similarity to the email address — and its
 * answer is the one that counts. Duplicating the full ruleset here would just
 * create two policies to keep in sync, and a client-side check is not a
 * security control in any case.
 */
function strengthOf(password: string): { score: number; label: string; tone: string } {
  if (!password) return { score: 0, label: "", tone: "" };
  let score = 0;
  if (password.length >= 12) score += 1;
  if (password.length >= 16) score += 1;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score += 1;
  if (/\d/.test(password)) score += 1;
  if (/[^\w\s]/.test(password)) score += 1;

  if (password.length < 12) return { score: 1, label: "Too short — 12 characters minimum", tone: "bg-err-500" };
  if (score <= 2) return { score: 2, label: "Weak", tone: "bg-err-500" };
  if (score === 3) return { score: 3, label: "Fair", tone: "bg-warn-500" };
  if (score === 4) return { score: 4, label: "Good", tone: "bg-ok-500" };
  return { score: 5, label: "Strong", tone: "bg-ok-500" };
}

export function Register() {
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
        err instanceof ApiError ? err.message : "Could not create the account. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthLayout
        title="Check your email"
        subtitle={
          <>
            We sent a confirmation link to <span className="text-ink-200">{email}</span>.
          </>
        }
        footer={
          <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
            Back to sign in
          </Link>
        }
      >
        <div className="space-y-4 text-[0.95rem] text-ink-400">
          <p>Open it to activate your account. The link works once and expires in an hour.</p>
          <p className="text-sm">
            Nothing arrived? Check spam, then try registering again — we will send a fresh link.
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
          <Link to="/login" className="font-medium text-brand-300 hover:text-brand-400">
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
            required
            placeholder="At least 12 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {password && (
            <div className="mt-2 flex items-center gap-2.5">
              <div className="flex h-1 flex-1 gap-1">
                {[1, 2, 3, 4, 5].map((i) => (
                  <span
                    key={i}
                    className={`h-full flex-1 rounded-full transition-colors duration-300 ${
                      i <= strength.score ? strength.tone : "bg-ink-700"
                    }`}
                  />
                ))}
              </div>
              <span className="w-44 flex-none text-right text-xs text-ink-400">
                {strength.label}
              </span>
            </div>
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
