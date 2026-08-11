import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import { ApiError, api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Spinner } from "./ui";

/**
 * The unconfirmed-email state, handled rather than announced.
 *
 * Saying "Email unconfirmed" and stopping is the shape of the problem this
 * replaces: it tells someone something is wrong, does not say what it costs
 * them, and gives them nothing to press.
 *
 * So this says what confirming unlocks and offers to resend — and is careful
 * about what it claims afterwards. The API answers 202 whether or not the mail
 * actually went out, because it must not reveal whether an address exists. The
 * browser therefore cannot know that anything was delivered, and saying "Sent"
 * would be asserting something unknowable: when delivery is misconfigured, the
 * user waits for a message that was never going to arrive and assumes the
 * fault is theirs. It reports the request, and names the other explanation.
 */
export function VerifyBanner() {
  const { user, reload } = useAuth();
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [dismissed, setDismissed] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);

  if (!user || user.is_verified || dismissed) return null;

  async function resend() {
    setState("sending");
    setDetail(null);
    try {
      await api.post("/auth/request-verify-token", { email: user!.email });
      setState("sent");
      // The address may have been confirmed in another tab while this sat open.
      void reload();
    } catch (err) {
      setState("failed");
      setDetail(
        err instanceof ApiError
          ? err.message
          : "The request did not go through. Please try again shortly.",
      );
    }
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, height: 0 }}
        className="surface mb-6 rounded-2xl border-warn-500/30 bg-warn-500/[0.06] p-5"
      >
        <div className="flex flex-wrap items-start gap-4">
          <span
            className="mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg"
            style={{ backgroundColor: "rgba(217,119,6,0.14)", color: "#d97706" }}
            aria-hidden="true"
          >
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
              <path
                d="M3 7l9 6 9-6M3 7v10h18V7M3 7l9-4 9 4"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>

          <div className="min-w-0 flex-1">
            <p className="font-medium text-ink-100">Confirm your email address</p>
            <p className="mt-1 text-sm leading-relaxed text-ink-400">
              You can keep using your account — this is not a lock. Confirming{" "}
              <span className="text-ink-200">{user.email}</span> is what makes
              password recovery and run notifications possible; without it, a
              forgotten password cannot be reset.
            </p>

            {state === "sent" && (
              <p className="mt-3 text-sm text-ink-300">
                <span style={{ color: "#059669" }}>Requested.</span> Check your
                inbox and spam — the link works once and expires in an hour.
                <br />
                <span className="text-ink-500">
                  Nothing after a few minutes means delivery is not configured
                  on this deployment, not that you did anything wrong. An
                  administrator can confirm the address directly.
                </span>
              </p>
            )}

            {state === "failed" && (
              <p className="mt-3 text-sm" style={{ color: "#e11d48" }}>
                {detail}
              </p>
            )}
          </div>

          <div className="flex flex-none items-center gap-2">
            <Button variant="secondary" onClick={resend} disabled={state === "sending"}>
              {state === "sending" ? (
                <>
                  <Spinner /> Sending
                </>
              ) : state === "sent" ? (
                "Send again"
              ) : (
                "Resend link"
              )}
            </Button>
            <button
              onClick={() => setDismissed(true)}
              aria-label="Dismiss"
              className="rounded-lg p-2 text-ink-500 transition-colors hover:bg-ink-800 hover:text-ink-200"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" aria-hidden="true">
                <path
                  d="M6 6l12 12M18 6L6 18"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
