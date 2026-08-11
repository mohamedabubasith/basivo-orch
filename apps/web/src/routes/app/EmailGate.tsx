/**
 * The wall an unconfirmed account meets instead of the workspace.
 *
 * This replaced a banner sitting above a fully working dashboard, which was
 * incoherent in both directions: if confirmation is optional the banner is
 * nagging, and if it is required the dashboard should not have been there. The
 * API now refuses org-scoped routes outright (see `basivo_orch/gate.py`), so
 * this screen is the honest picture of that rule rather than a decoration on
 * top of it.
 *
 * It is not a dead end. Resend, check again, use a different address, or sign
 * out — every exit a person stuck here might actually need.
 */

import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Alert, Button, Logo, Spinner } from "../../components/ui";

export function EmailGate() {
  const { user, reload, signOut } = useAuth();
  const navigate = useNavigate();
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [detail, setDetail] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  // The confirmation link is usually opened in another tab. Re-checking on
  // focus means coming back to this one shows the workspace instead of a wall
  // the user has already satisfied.
  useEffect(() => {
    const onFocus = () => void reload();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reload]);

  async function resend() {
    setState("sending");
    setDetail(null);
    try {
      await api.post("/auth/request-verify-token", { email: user!.email });
      setState("sent");
    } catch (err) {
      setState("failed");
      setDetail(
        err instanceof ApiError
          ? err.message
          : "The request did not go through. Please try again shortly.",
      );
    }
  }

  async function checkAgain() {
    setChecking(true);
    const me = await reload();
    setChecking(false);
    if (me?.is_verified) navigate("/app", { replace: true });
  }

  return (
    <div className="grid-bg grid min-h-dvh place-items-center px-5 py-16">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.21, 0.5, 0.35, 1] }}
        className="surface w-full max-w-lg rounded-3xl p-8 sm:p-10"
      >
        <Logo />

        <motion.div
          initial={{ scale: 0.9, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.1, type: "spring", stiffness: 260, damping: 20 }}
          className="mt-8 grid h-12 w-12 place-items-center rounded-2xl"
          style={{ backgroundColor: "rgba(217,119,6,0.14)", color: "#d97706" }}
          aria-hidden="true"
        >
          <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none">
            <path
              d="M3 7l9 6 9-6M3 7v10h18V7M3 7l9-4 9 4"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </motion.div>

        <h1 className="mt-5 text-2xl font-semibold tracking-tight text-ink-100">
          Confirm your email to continue
        </h1>
        <p className="mt-3 leading-relaxed text-ink-400">
          We sent a link to <span className="text-ink-200">{user?.email}</span>.
          Opening it unlocks your workspace.
        </p>
        <p className="mt-3 text-sm leading-relaxed text-ink-500">
          A workspace is addressed by email — invitations, ownership and
          password recovery all resolve to it. Until someone has proved they
          can read that mailbox, there is no safe way to hand them one.
        </p>

        {state === "sent" && (
          <div className="mt-5">
            <Alert tone="info">
              Requested. The link works once and expires in an hour — check spam
              too. If nothing arrives within a few minutes, email delivery is
              not configured on this deployment; that is a server problem, not
              something you did wrong.
            </Alert>
          </div>
        )}
        {state === "failed" && (
          <div className="mt-5">
            <Alert>{detail}</Alert>
          </div>
        )}

        <div className="mt-7 flex flex-wrap gap-2.5">
          <Button onClick={() => void checkAgain()} loading={checking}>
            I&rsquo;ve confirmed — check again
          </Button>
          <Button variant="secondary" onClick={() => void resend()} disabled={state === "sending"}>
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
        </div>

        <div className="mt-7 border-t border-ink-700/60 pt-5 text-sm text-ink-500">
          Wrong address, or no longer have access to it?{" "}
          <button
            onClick={() => void signOut()}
            className="text-ink-300 underline decoration-dotted underline-offset-2 transition-colors hover:text-ink-100"
          >
            Sign out
          </button>{" "}
          and register with one you can read.
        </div>
      </motion.div>
    </div>
  );
}
