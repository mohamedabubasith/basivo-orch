import { type FormEvent, type ReactNode, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import {
  Alert,
  Button,
  Card,
  Field,
  IconChip,
  Modal,
  Pill,
  Section,
} from "../../components/ui";
import { PageHeader } from "./bits";

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

const GOOD = "var(--status-good)";
const WARN = "var(--status-warn)";

// One glyph per section, reused by the status strip so the same thing wears
// the same icon at the top of the page and further down.
const ICONS = {
  mail: (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect
        x="3"
        y="5"
        width="18"
        height="14"
        rx="2.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        d="m3.5 7.5 7.3 5.2a2 2 0 0 0 2.4 0l7.3-5.2"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  shield: (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3 4.5 6v5.3c0 4.4 3.1 8.1 7.5 9.7 4.4-1.6 7.5-5.3 7.5-9.7V6L12 3Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
      <path
        d="m9 12.2 2 2 4-4.4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  key: (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="8" cy="15.5" r="4" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="m11 12.5 8.5-8.5M16 6.5 18.5 9M13.5 9l2.5 2.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
};

export function Security() {
  const { user } = useAuth();
  const verified = Boolean(user?.is_verified);
  const totp = Boolean(user?.totp_enabled);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Account"
        title="Security"
        subtitle="Protect the account that can run your pipelines."
      />

      <Card className="grid divide-y divide-[var(--edge)] md:grid-cols-3 md:divide-x md:divide-y-0">
        <StatusTile
          icon={ICONS.mail}
          hue={verified ? GOOD : WARN}
          label="Email"
          pill={
            <Pill tone={verified ? "good" : "warn"} dot>
              {verified ? "Confirmed" : "Not confirmed"}
            </Pill>
          }
          hint={user?.email}
        />
        <StatusTile
          icon={ICONS.shield}
          hue={totp ? GOOD : WARN}
          label="Two-factor"
          pill={
            <Pill tone={totp ? "good" : "warn"} dot>
              {totp ? "On" : "Off"}
            </Pill>
          }
          hint={
            totp
              ? "A code from your app is asked for at sign-in."
              : "Your password alone signs you in."
          }
        />
        <StatusTile
          icon={ICONS.key}
          label="Password"
          pill={<Pill dot>Set</Pill>}
          hint="Changing it signs out other devices."
        />
      </Card>

      <EmailSection />
      <TwoFactorSection />
      <ChangePasswordSection />
    </div>
  );
}

function StatusTile({
  icon,
  hue,
  label,
  pill,
  hint,
}: {
  icon: ReactNode;
  hue?: string;
  label: string;
  pill: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="flex items-start gap-3.5 p-5">
      <IconChip hue={hue}>{icon}</IconChip>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm font-medium text-ink-100">{label}</p>
          {pill}
        </div>
        {hint && <p className="mt-1 truncate text-xs text-ink-400">{hint}</p>}
      </div>
    </div>
  );
}

/** The number beside an enrolment step. Decorative: the step text carries the meaning. */
function StepNo({ n }: { n: number }) {
  return (
    <span
      aria-hidden="true"
      className="grid h-6 w-6 flex-none place-items-center rounded-full bg-ink-800 text-xs font-semibold text-ink-200"
    >
      {n}
    </span>
  );
}

/* --------------------------------------------------------------- email --- */

function EmailSection() {
  const { user, reload } = useAuth();
  const verified = Boolean(user?.is_verified);
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">(
    "idle",
  );
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
        err instanceof ApiError
          ? err.message
          : "The request did not go through.",
      );
    }
  }

  return (
    <Section
      icon={ICONS.mail}
      hue={verified ? GOOD : WARN}
      title="Email address"
      description={
        <>
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            <span className="truncate font-medium text-ink-100">
              {user?.email}
            </span>
            <Pill tone={verified ? "good" : "warn"} dot>
              {verified ? "Confirmed" : "Not confirmed"}
            </Pill>
          </div>
          <p className="mt-1.5 max-w-xl">
            {verified
              ? "Password reset links are sent here."
              : "Your account works either way. Confirming is what makes password recovery possible. Without it, a forgotten password cannot be reset, because the reset link has nowhere verified to go."}
          </p>
        </>
      }
      action={
        !verified && (
          <Button
            variant="secondary"
            onClick={() => void resend()}
            disabled={state === "sending"}
            loading={state === "sending"}
          >
            {state === "sent" ? "Send again" : "Send confirmation link"}
          </Button>
        )
      }
    >
      {state === "sent" && (
        <div className="mt-5">
          <Alert tone="info">
            Requested for {user?.email}. The link works once and expires in an
            hour. If nothing arrives within a few minutes, email delivery is not
            configured on this deployment. An administrator can confirm the
            address directly.
          </Alert>
        </div>
      )}
      {state === "failed" && (
        <div className="mt-5">
          <Alert>{detail}</Alert>
        </div>
      )}
    </Section>
  );
}

/* ----------------------------------------------------------------- 2fa --- */

function TwoFactorSection() {
  const { user, reload } = useAuth();
  const on = Boolean(user?.totp_enabled);
  const [enrol, setEnrol] = useState<EnrolStart | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [copied, setCopied] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function start() {
    setError(null);
    setBusy(true);
    try {
      setEnrol(await api.post<EnrolStart>("/auth/2fa/enrol"));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not start enrolment.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function confirm(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = await api.post<{ recovery_codes: string[] }>(
        "/auth/2fa/enrol/confirm",
        {
          code: code.trim(),
        },
      );
      setRecoveryCodes(result.recovery_codes);
      setEnrol(null);
      setCode("");
      await reload();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That code was not accepted.",
      );
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
      setError(
        err instanceof ApiError ? err.message : "Could not turn off 2FA.",
      );
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    setEnrol(null);
    setCode("");
    setError(null);
  }

  function dismissCodes() {
    setRecoveryCodes(null);
    setCopied(false);
  }

  return (
    <>
      <Section
        icon={ICONS.shield}
        hue={on ? GOOD : WARN}
        title="Two-factor authentication"
        description={
          on
            ? "On. You are asked for a code from your authenticator app after your password."
            : "Off. Add a code from an authenticator app to your sign-in."
        }
        action={
          !enrol &&
          (on ? (
            <Button
              variant="secondary"
              loading={busy}
              onClick={() => void disable()}
            >
              Turn off
            </Button>
          ) : (
            <Button loading={busy} onClick={() => void start()}>
              Set up
            </Button>
          ))
        }
      >
        {error && (
          <div className="mt-5">
            <Alert>{error}</Alert>
          </div>
        )}

        {enrol && (
          <div className="mt-6 border-t border-[var(--edge)] pt-6">
            <div className="grid gap-6 sm:grid-cols-[auto_1fr]">
              <div
                className="mx-auto w-44 rounded-2xl bg-white p-3 shadow-[0_1px_2px_rgba(0,0,0,0.25)] [&_svg]:h-auto [&_svg]:w-full"
                // The SVG is generated server-side from the provisioning URI —
                // markup we produced, not user input, with no script execution
                // path. `svgBody` drops the XML prolog, which is a parse error in
                // an HTML document, and the fixed mm dimensions, which would
                // otherwise render a QR code sized for a printer.
                dangerouslySetInnerHTML={{ __html: svgBody(enrol.qr_code_svg) }}
              />

              <div className="space-y-5">
                <div className="flex gap-3">
                  <StepNo n={1} />
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-ink-100">
                      Scan with your authenticator app
                    </p>
                    <p className="mt-1 text-sm text-ink-400">
                      1Password, Authy, Google Authenticator, any TOTP app
                      works.
                    </p>
                    <details className="mt-2">
                      <summary className="cursor-pointer text-sm text-ink-300 hover:text-ink-100">
                        Can&rsquo;t scan? Enter the key manually
                      </summary>
                      <code className="mt-2 block rounded-lg border border-[var(--edge)] bg-ink-900/70 p-2.5 font-mono text-xs break-all text-ink-100 select-all">
                        {enrol.secret}
                      </code>
                    </details>
                  </div>
                </div>

                <form onSubmit={confirm} className="flex gap-3">
                  <StepNo n={2} />
                  <div className="min-w-0 flex-1 space-y-3">
                    <div className="max-w-xs">
                      <Field
                        label="Enter the 6-digit code it shows"
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
                    </div>
                    <div className="flex gap-2">
                      <Button
                        type="submit"
                        loading={busy}
                        disabled={code.trim().length < 6}
                      >
                        Confirm and enable
                      </Button>
                      <Button type="button" variant="ghost" onClick={cancel}>
                        Cancel
                      </Button>
                    </div>
                  </div>
                </form>
              </div>
            </div>
          </div>
        )}
      </Section>

      {/* Shown exactly once. The server keeps only hashes, so there is no
          screen that can ever show these again. */}
      {recoveryCodes && (
        <Modal
          title="Save your recovery codes"
          description="Each works once, and this is the only time they are shown, only their hashes are stored. Keep them somewhere other than the device with your authenticator."
          onClose={dismissCodes}
          footer={
            <>
              <Button
                variant="secondary"
                onClick={() => {
                  void navigator.clipboard?.writeText(
                    recoveryCodes.join("\n"),
                  );
                  setCopied(true);
                }}
              >
                {copied ? "Copied" : "Copy all"}
              </Button>
              <Button onClick={dismissCodes}>I have saved them</Button>
            </>
          }
        >
          <ul className="grid grid-cols-2 gap-x-6 gap-y-2.5 rounded-xl border border-[var(--edge)] bg-ink-900/70 p-4 font-mono text-sm text-ink-100 tabular-nums sm:grid-cols-3">
            {recoveryCodes.map((rc) => (
              <li key={rc}>{rc}</li>
            ))}
          </ul>
        </Modal>
      )}
    </>
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
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not change the password.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section
      icon={ICONS.key}
      title="Password"
      description="Changing it revokes every other session, on every device."
    >
      <form
        onSubmit={onSubmit}
        className="mt-6 border-t border-[var(--edge)] pt-6"
        noValidate
      >
        <div className="grid max-w-2xl gap-4 sm:grid-cols-2">
          {error && (
            <div className="sm:col-span-2">
              <Alert>{error}</Alert>
            </div>
          )}
          {done && (
            <div className="sm:col-span-2">
              <Alert tone="success">
                Password updated. Other devices have been signed out.
              </Alert>
            </div>
          )}

          <Field
            label="Current password"
            type="password"
            revealable
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
          <Field
            label="New password"
            type="password"
            revealable
            autoComplete="new-password"
            required
            hint="At least 12 characters"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
          <div className="sm:col-span-2">
            <Button type="submit" loading={busy} disabled={!current || !next}>
              Change password
            </Button>
          </div>
        </div>
      </form>
    </Section>
  );
}
