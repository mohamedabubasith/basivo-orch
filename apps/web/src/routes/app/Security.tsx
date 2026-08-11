import { type FormEvent, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Alert, Button, Card, Field } from "../../components/ui";

interface EnrolStart {
  secret: string;
  provisioning_uri: string;
  qr_code_svg: string;
}

/**
 * Make a standalone SVG document safe to drop into an HTML page.
 *
 * The API renders the QR code with a full XML prolog and physical dimensions
 * (`width="57mm"`), which is correct for a `.svg` file and wrong inside HTML:
 * the prolog is a parse error, and millimetres are sized for paper. Strip both
 * and let CSS size it.
 */
function svgBody(svg: string): string {
  return svg
    .replace(/<\?xml[^>]*\?>/gi, "")
    .replace(/<!DOCTYPE[^>]*>/gi, "")
    .replace(/\s(width|height)="[\d.]+(mm|cm|in|pt)"/gi, "")
    .trim();
}

export function Security() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-100">Security</h1>
        <p className="mt-1.5 text-ink-400">Protect the account that can run your pipelines.</p>
      </div>
      <EmailSection />
      <TwoFactorSection />
      <ChangePasswordSection />
    </div>
  );
}

/* --------------------------------------------------------------- email --- */

function EmailSection() {
  const { user, reload } = useAuth();
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [detail, setDetail] = useState<string | null>(null);

  async function resend() {
    setState("sending");
    setDetail(null);
    try {
      await api.post("/auth/request-verify-token", { email: user!.email });
      setState("sent");
      void reload();
    } catch (err) {
      setState("failed");
      setDetail(
        err instanceof ApiError ? err.message : "The request did not go through.",
      );
    }
  }

  return (
    <Card className="p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-ink-100">Email address</h2>
          <p className="mt-1.5 truncate text-sm text-ink-300">{user?.email}</p>

          {user?.is_verified ? (
            <p className="mt-2 flex items-center gap-1.5 text-sm" style={{ color: "var(--status-good)" }}>
              <svg viewBox="0 0 12 12" className="h-3.5 w-3.5" aria-hidden="true">
                <path
                  d="M2.5 6.4 4.8 8.7 9.5 3.9"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Confirmed
            </p>
          ) : (
            <p className="mt-2 max-w-md text-sm leading-relaxed text-ink-400">
              <span style={{ color: "var(--status-warn)" }}>Not confirmed.</span> Your
              account works either way — confirming is what makes password
              recovery possible. Without it, a forgotten password cannot be
              reset, because the reset link has nowhere verified to go.
            </p>
          )}
        </div>

        {!user?.is_verified && (
          <Button variant="secondary" onClick={resend} disabled={state === "sending"} loading={state === "sending"}>
            {state === "sent" ? "Send again" : "Send confirmation link"}
          </Button>
        )}
      </div>

      {state === "sent" && (
        <div className="mt-4">
          <Alert tone="info">
            Requested for {user?.email}. The link works once and expires in an
            hour. If nothing arrives within a few minutes, email delivery is not
            configured on this deployment — an administrator can confirm the
            address directly.
          </Alert>
        </div>
      )}
      {state === "failed" && (
        <div className="mt-4">
          <Alert>{detail}</Alert>
        </div>
      )}
    </Card>
  );
}

/* ----------------------------------------------------------------- 2fa --- */

function TwoFactorSection() {
  const { user, reload } = useAuth();
  const [enrol, setEnrol] = useState<EnrolStart | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function start() {
    setError(null);
    setBusy(true);
    try {
      setEnrol(await api.post<EnrolStart>("/auth/2fa/enrol"));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start enrolment.");
    } finally {
      setBusy(false);
    }
  }

  async function confirm(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await api.post<{ recovery_codes: string[] }>("/auth/2fa/enrol/confirm", {
        code: code.trim(),
      });
      setRecoveryCodes(result.recovery_codes);
      setEnrol(null);
      setCode("");
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That code was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setError(null);
    setBusy(true);
    try {
      await api.post("/auth/2fa/disable");
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not turn off 2FA.");
    } finally {
      setBusy(false);
    }
  }

  // Shown exactly once. The server keeps only hashes, so there is no screen
  // that can ever show these again.
  if (recoveryCodes) {
    return (
      <Card className="p-6">
        <h2 className="text-lg font-semibold text-ink-100">Save your recovery codes</h2>
        <p className="mt-1.5 text-sm text-ink-400">
          Each works once, and this is the only time they are shown — only their hashes are
          stored. Keep them somewhere other than the device with your authenticator.
        </p>
        <ul className="mt-4 grid grid-cols-2 gap-2 rounded-xl border border-ink-700/70 bg-ink-950/60 p-4 font-mono text-sm text-ink-200 sm:grid-cols-3">
          {recoveryCodes.map((rc) => (
            <li key={rc}>{rc}</li>
          ))}
        </ul>
        <div className="mt-4 flex gap-2">
          <Button
            variant="secondary"
            onClick={() => void navigator.clipboard?.writeText(recoveryCodes.join("\n"))}
          >
            Copy all
          </Button>
          <Button onClick={() => setRecoveryCodes(null)}>I have saved them</Button>
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-ink-100">Two-factor authentication</h2>
          <p className="mt-1.5 text-sm text-ink-400">
            {user?.totp_enabled
              ? "On. You are asked for a code from your authenticator app after your password."
              : "Off. Add a code from an authenticator app to your sign-in."}
          </p>
        </div>
        {!enrol &&
          (user?.totp_enabled ? (
            <Button variant="secondary" loading={busy} onClick={() => void disable()}>
              Turn off
            </Button>
          ) : (
            <Button loading={busy} onClick={() => void start()}>
              Set up
            </Button>
          ))}
      </div>

      {error && <div className="mt-4">{<Alert>{error}</Alert>}</div>}

      {enrol && (
        <div className="mt-6 border-t border-ink-700/70 pt-6">
          <div className="grid gap-6 sm:grid-cols-[auto_1fr]">
            <div
              className="mx-auto w-40 rounded-xl bg-white p-3 [&_svg]:h-auto [&_svg]:w-full"
              // The SVG is generated server-side from the provisioning URI —
              // markup we produced, not user input, with no script execution
              // path. `svgBody` drops the XML prolog, which is a parse error in
              // an HTML document, and the fixed mm dimensions, which would
              // otherwise render a QR code sized for a printer.
              dangerouslySetInnerHTML={{ __html: svgBody(enrol.qr_code_svg) }}
            />

            <div className="space-y-4">
              <div>
                <p className="text-sm font-medium text-ink-200">1. Scan with your app</p>
                <p className="mt-1 text-sm text-ink-400">
                  1Password, Authy, Google Authenticator — any TOTP app works.
                </p>
                <details className="mt-2">
                  <summary className="cursor-pointer text-sm text-ink-400 hover:text-ink-200">
                    Can&rsquo;t scan? Enter the key manually
                  </summary>
                  <code className="mt-2 block rounded-lg border border-ink-700/70 bg-ink-950/60 p-2.5 font-mono text-xs break-all text-ink-200">
                    {enrol.secret}
                  </code>
                </details>
              </div>

              <form onSubmit={confirm} className="space-y-3">
                <Field
                  label="2. Enter the 6-digit code it shows"
                  name="code"
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  required
                  autoFocus
                  placeholder="000000"
                  maxLength={10}
                  className="font-mono tracking-[0.3em]"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
                <div className="flex gap-2">
                  <Button type="submit" loading={busy} disabled={code.trim().length < 6}>
                    Confirm and enable
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => {
                      setEnrol(null);
                      setCode("");
                      setError(null);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------ password --- */

function ChangePasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setDone(false);
    setBusy(true);
    try {
      await api.post("/auth/change-password", {
        current_password: current,
        new_password: next,
      });
      setDone(true);
      setCurrent("");
      setNext("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-6">
      <h2 className="text-lg font-semibold text-ink-100">Password</h2>
      <p className="mt-1.5 text-sm text-ink-400">
        Changing it revokes every other session, on every device.
      </p>

      <form onSubmit={onSubmit} className="mt-5 max-w-sm space-y-4" noValidate>
        {error && <Alert>{error}</Alert>}
        {done && <Alert tone="success">Password updated.</Alert>}

        <Field
          label="Current password"
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <Field
          label="New password"
          type="password"
          autoComplete="new-password"
          required
          placeholder="At least 12 characters"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
        <Button type="submit" loading={busy} disabled={!current || !next}>
          Change password
        </Button>
      </form>
    </Card>
  );
}
