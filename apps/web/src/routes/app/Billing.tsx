/**
 * The plan, what has been used against it, and how to buy a bigger one.
 *
 * Two states, decided by the server and never guessed at here. In demo mode
 * the plans are a preview: nothing is enforced, nothing can be charged, and
 * the page says exactly that once, at the top, instead of showing buttons
 * that would fail. In production mode the usage meters are real and the
 * upgrade button opens the payment provider's own checkout, which is where
 * the card details go so they never touch this application at all.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, api, isSessionEnded } from "../../lib/api";
import { cx } from "../../lib/cx";
import { useWorkspace } from "../../lib/workspace";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  PageLoader,
  Pill,
  type Tone,
} from "../../components/ui";
import { PageHeader } from "./bits";

interface Plan {
  code: string;
  name: string;
  tagline: string;
  price_inr: string;
  price_usd: string;
  runs_per_month: number | null;
  flows: number | null;
  apps: number | null;
  seats: number | null;
  history_days: number;
  storage_mb: number | null;
  features: string[];
}

interface Overview {
  mode: "demo" | "production";
  plan: Plan;
  status: string;
  usage: {
    runs_used: number;
    runs_limit: number | null;
    flows_used: number;
    flows_limit: number | null;
    apps_used: number;
    apps_limit: number | null;
    seats_used: number;
    seats_limit: number | null;
    storage_used_mb: number;
    storage_limit_mb: number | null;
    history_days: number;
  };
  current_period_end: string | null;
  grace_until: string | null;
  cancel_at_period_end: boolean;
  can_manage: boolean;
  plans: Plan[];
}

const STATUS_LABEL: Record<string, { label: string; tone: Tone }> = {
  active: { label: "Active", tone: "good" },
  past_due: { label: "Payment failed", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "warn" },
  expired: { label: "Ended", tone: "neutral" },
};

function day(value: string | null): string {
  if (!value) return "";
  return new Date(value).toLocaleDateString(undefined, {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

function limitText(limit: number | null): string {
  return limit === null ? "Unlimited" : limit.toLocaleString();
}

/** One usage line: a number, its allowance, and a bar that fills up. */
function Meter({
  label,
  used,
  limit,
  hint,
}: {
  label: string;
  /** Undefined when the API has not been upgraded yet. See below. */
  used: number | undefined;
  limit: number | null | undefined;
  hint?: string;
}) {
  // An API one deploy behind this bundle does not send a field this page was
  // written for, and reading a number off `undefined` took the whole billing
  // page down rather than one meter. A missing figure is a missing meter.
  if (typeof used !== "number") return null;
  const ceiling = limit ?? null;
  const share = ceiling === null ? 0 : Math.min(used / Math.max(ceiling, 1), 1);
  const tone =
    ceiling === null || share < 0.8
      ? "var(--status-good)"
      : share < 1
        ? "var(--status-warn)"
        : "var(--status-bad)";

  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm font-medium text-ink-200">{label}</p>
        <p className="font-mono text-sm text-ink-300">
          {used.toLocaleString()}
          <span className="text-ink-500"> / {limitText(ceiling)}</span>
        </p>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-ink-800">
        <div
          className="h-full rounded-full transition-[width] duration-500"
          style={{
            width: limit === null ? "100%" : `${Math.max(share * 100, 2)}%`,
            background:
              limit === null
                ? "color-mix(in oklab, var(--status-good) 45%, transparent)"
                : tone,
          }}
        />
      </div>
      {hint && <p className="mt-1.5 text-xs text-ink-500">{hint}</p>}
    </div>
  );
}

function PlanCard({
  plan,
  current,
  mode,
  busy,
  onChoose,
}: {
  plan: Plan;
  current: boolean;
  mode: "demo" | "production";
  busy: boolean;
  onChoose: (code: string) => void;
}) {
  const paid = plan.code !== "free";
  return (
    <Card
      className={cx(
        "flex flex-col p-6",
        current && "ring-1 ring-brand-400/40",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-ink-100">{plan.name}</h2>
          <p className="mt-1 text-sm text-ink-400">{plan.tagline}</p>
        </div>
        {current && <Pill tone="info">Current plan</Pill>}
      </div>

      <p className="mt-5">
        <span className="text-2xl font-semibold text-ink-100">
          {plan.price_inr}
        </span>
        {paid && <span className="text-sm text-ink-500"> a month</span>}
      </p>
      {paid && (
        <p className="mt-1 text-xs text-ink-500">
          {plan.price_usd} a month if you pay in dollars
        </p>
      )}

      <ul className="mt-5 space-y-2 text-sm text-ink-300">
        {plan.features.map((feature) => (
          <li key={feature} className="flex gap-2.5">
            <svg
              viewBox="0 0 24 24"
              className="mt-0.5 h-4 w-4 flex-none text-brand-400"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M4.5 12.5l5 5 10-10" />
            </svg>
            <span>{feature}</span>
          </li>
        ))}
      </ul>

      <div className="mt-6 flex-1" />
      {paid && !current && (
        <Button
          variant={mode === "demo" ? "secondary" : "primary"}
          onClick={() => onChoose(plan.code)}
          loading={busy}
          disabled={mode === "demo"}
        >
          {mode === "demo" ? "Not for sale yet" : `Upgrade to ${plan.name}`}
        </Button>
      )}
      {current && (
        <p className="text-sm text-ink-500">This is what you are on today.</p>
      )}
    </Card>
  );
}

export function Billing() {
  const { orgId } = useWorkspace();
  const [view, setView] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setView(await api.get<Overview>(`/api/v1/orgs/${orgId}/billing`));
      setError(null);
    } catch (err) {
      // A 401 is the session ending, and the session handler is already
      // taking them to the sign-in screen. Saying the data failed sends
      // somebody looking for a problem that is not there.
      if (isSessionEnded(err)) return;
      setError("Could not load your plan.");
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // The provider sends the customer back here after checkout. The plan
    // itself arrives by webhook a moment later, so the page says what is
    // happening rather than showing the old plan as though nothing changed.
    const params = new URLSearchParams(window.location.search);
    if (params.get("checkout") === "done") {
      setNotice(
        "Thank you. Your payment is being confirmed and the new plan appears here within a minute.",
      );
      window.history.replaceState({}, "", window.location.pathname);
      const again = window.setTimeout(() => void load(), 8000);
      return () => window.clearTimeout(again);
    }
  }, [load]);

  /**
   * Show a failure where it will be seen.
   *
   * The upgrade buttons sit at the bottom of a long page and the message
   * belongs at the top, so without the scroll a failed checkout looks like a
   * button that did nothing at all.
   */
  function report(message: string) {
    setError(message);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function choose(code: string) {
    if (!orgId) return;
    setBusy(code);
    setError(null);
    try {
      const { checkout_url } = await api.post<{ checkout_url: string }>(
        `/api/v1/orgs/${orgId}/billing/checkout`,
        { plan: code },
      );
      window.location.href = checkout_url;
    } catch (err) {
      report(
        err instanceof ApiError ? err.message : "Could not open the checkout.",
      );
      setBusy(null);
    }
  }

  async function manage() {
    if (!orgId) return;
    setBusy("portal");
    setError(null);
    try {
      const { portal_url } = await api.post<{ portal_url: string }>(
        `/api/v1/orgs/${orgId}/billing/portal`,
      );
      window.location.href = portal_url;
    } catch (err) {
      report(
        err instanceof ApiError
          ? err.message
          : "Could not open your billing page.",
      );
      setBusy(null);
    }
  }

  if (!view && error) return <Alert>{error}</Alert>;
  if (!view) return <PageLoader label="Loading your plan" />;

  const { usage, plan, mode } = view;
  const status = STATUS_LABEL[view.status] ?? {
    label: view.status,
    tone: "neutral" as Tone,
  };

  return (
    <div className="space-y-8">
      <PageHeader
        eyebrow="Account"
        title="Plan and usage"
        subtitle="What this workspace is on, what it has used this month, and what else is available."
        action={
          view.can_manage && mode === "production" ? (
            <Button
              variant="secondary"
              onClick={() => void manage()}
              loading={busy === "portal"}
            >
              Manage subscription
            </Button>
          ) : undefined
        }
      />

      {mode === "demo" && (
        <Alert tone="info">
          Billing is switched off in this deployment. The plans below are a
          preview, no limit is applied, and nothing can be charged.
        </Alert>
      )}
      {notice && <Alert tone="success">{notice}</Alert>}
      {error && <Alert>{error}</Alert>}

      {view.status === "past_due" && view.grace_until && (
        <Alert>
          A payment did not go through. Your {plan.name} plan keeps working
          until {day(view.grace_until)}. Update your card on the billing page to
          keep it.
        </Alert>
      )}
      {view.cancel_at_period_end && view.current_period_end && (
        <Alert tone="info">
          This subscription is cancelled. The {plan.name} plan runs until{" "}
          {day(view.current_period_end)}, then the workspace goes back to Free.
          Nothing is deleted.
        </Alert>
      )}

      <Card className="p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-xs font-medium tracking-[0.14em] text-ink-400 uppercase">
              Current plan
            </p>
            <p className="mt-1.5 text-xl font-semibold text-ink-100">
              {plan.name}
            </p>
            <p className="mt-1 text-sm text-ink-400">{plan.tagline}</p>
          </div>
          <div className="flex items-center gap-2">
            {mode === "production" && <Pill tone={status.tone}>{status.label}</Pill>}
            {view.current_period_end && !view.cancel_at_period_end && (
              <span className="text-sm text-ink-500">
                Renews {day(view.current_period_end)}
              </span>
            )}
          </div>
        </div>

        <div className="mt-6 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          <Meter
            label="Runs this month"
            used={usage.runs_used}
            limit={usage.runs_limit}
            hint="The count resets on the first of each month."
          />
          <Meter label="Flows" used={usage.flows_used} limit={usage.flows_limit} />
          <Meter label="Apps" used={usage.apps_used} limit={usage.apps_limit} />
          <Meter
            label="Members"
            used={usage.seats_used}
            limit={usage.seats_limit}
          />
          <Meter
            label="Storage, MB"
            used={usage.storage_used_mb}
            limit={usage.storage_limit_mb}
            hint="Rendered files, app builds and the images uploaded to them."
          />
        </div>

        <p className="mt-6 text-sm text-ink-500">
          {mode === "demo"
            ? "Run history is not limited in this deployment."
            : `Run history goes back ${usage.history_days.toLocaleString()} days on this plan. Older runs are kept, they are just not listed.`}
        </p>
      </Card>

      <section className="space-y-4">
        <h2 className="text-sm font-medium tracking-[0.14em] text-ink-400 uppercase">
          Plans
        </h2>
        {view.plans.length === 0 ? (
          <EmptyState title="No plans are on sale yet">
            Get in touch and we will set one up for you.
          </EmptyState>
        ) : (
          <div className="grid gap-4 lg:grid-cols-3">
            {view.plans.map((option) => (
              <PlanCard
                key={option.code}
                plan={option}
                current={option.code === plan.code}
                mode={mode}
                busy={busy === option.code}
                onChoose={(code) => void choose(code)}
              />
            ))}
          </div>
        )}
        <p className="text-sm text-ink-500">
          Payments are handled by our payment provider, so your card details
          never reach this application. Prices include tax where it applies.
        </p>
      </section>
    </div>
  );
}
