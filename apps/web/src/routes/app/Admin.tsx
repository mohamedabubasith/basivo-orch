/**
 * The platform staff console.
 *
 * Everything here is about the whole installation rather than one workspace,
 * so it never asks the workspace picker for an org id. The API answers 404 to
 * anyone who is not staff, which is deliberate: a stranger learns nothing
 * about whether these routes exist. This page says so in plain words when it
 * happens, rather than showing a bare failure.
 */

import { motion } from "motion/react";
import {
  useCallback,
  useEffect,
  useId,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";

import { StatTile } from "../../components/charts";
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  Modal,
  Pill,
  Spinner,
  type Tone,
} from "../../components/ui";
import { ApiError, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatPercent } from "../../lib/viz";
import { PageHeader, RelativeTime, duration } from "./bits";

/* ---------------------------------------------------------------- types --- */

interface Overview {
  window_days: number;
  workspaces: {
    total: number;
    new: number;
    free: number;
    paying: number;
    by_plan: Record<string, number>;
  };
  people: { total: number; new: number; unverified: number };
  flows: { total: number };
  runs: {
    total: number;
    by_status: Record<string, number>;
    failure_rate: number;
    queued_now: number;
  };
  slowest_nodes: { node_type: string; runs: number; average_ms: number }[];
}

interface GroupedError {
  node_type: string;
  signature: string;
  count: number;
  run_id: string;
  last_seen: string | null;
  message: string;
}

interface RecentError {
  run_id: string;
  flow: string;
  workspace: string;
  organization_id: string;
  error: string;
  created_at: string | null;
  duration_ms: number | null;
}

interface ErrorFeed<Item> {
  kind: string;
  items: Item[];
}

interface AdminWorkspace {
  organization_id: string;
  name: string;
  slug: string;
  is_active: boolean;
  created_at: string | null;
  members: number;
  runs: number;
  plan: string;
  status: string;
}

interface Plan {
  code: string;
  name: string;
  tagline: string;
  price_inr: string;
  price_usd: string;
  runs_per_month: number | null;
  flows: number | null;
  seats: number | null;
  history_days: number | null;
  features: string[];
  overridden: string[];
  product_id: string | null;
}

/* --------------------------------------------------------------- pieces --- */

const NOT_STAFF =
  "This area is for platform staff. Your account does not have access to it.";

/**
 * One sentence a person can read, for any failure.
 *
 * A 404 from these routes is not a missing page, it is the API declining to
 * confirm they exist to a caller who is not staff. Everything else falls back
 * to the caller's own plain sentence rather than a thrown object.
 */
function failureText(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 404 || err.status === 403) return NOT_STAFF;
    if (err.isUserFixable && err.message) return err.message;
  }
  return fallback;
}

function Loading({ label }: { label: string }) {
  return (
    <div
      className="flex items-center gap-3 py-16 text-ink-400"
      role="status"
      aria-live="polite"
    >
      <Spinner className="h-5 w-5" />
      <span className="text-sm">{label}</span>
    </div>
  );
}

/** A limit as a person reads it. Null and any negative value mean no limit. */
function limitText(value: number | null): string {
  if (value === null || value < 0) return "Unlimited";
  return value.toLocaleString();
}

/** A price is whatever staff typed, symbol included, so it is shown as it is. */
function priceText(value: string): string {
  return value.trim() || "Not set";
}

const STATUS_TONES: Record<string, Tone> = {
  active: "good",
  trialing: "info",
  past_due: "warn",
  cancelled: "neutral",
  none: "neutral",
};

function statusTone(status: string): Tone {
  return STATUS_TONES[status.toLowerCase()] ?? "neutral";
}

const TABS = [
  { key: "product", label: "Product" },
  { key: "errors", label: "Errors" },
  { key: "workspaces", label: "Workspaces" },
  { key: "pricing", label: "Pricing" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

const WINDOWS = [1, 7, 30] as const;

/**
 * The segmented switcher the dashboard uses for its day window, with the
 * moving pill. One component, two callers, so the two rows of buttons cannot
 * drift apart.
 */
function Switcher<Value extends string | number>({
  options,
  value,
  onChange,
  layoutId,
}: {
  options: readonly { key: Value; label: string }[];
  value: Value;
  onChange: (next: Value) => void;
  layoutId: string;
}) {
  return (
    <div className="flex items-center gap-1 rounded-xl border border-ink-700/60 bg-ink-900/50 p-1">
      {options.map((option) => (
        <button
          key={String(option.key)}
          type="button"
          onClick={() => onChange(option.key)}
          aria-pressed={value === option.key}
          className={`relative rounded-lg px-3 py-1.5 text-sm transition-colors ${
            value === option.key
              ? "text-ink-100"
              : "text-ink-400 hover:text-ink-200"
          }`}
        >
          {value === option.key && (
            <motion.span
              layoutId={layoutId}
              className="absolute inset-0 rounded-lg bg-ink-800"
              transition={{ type: "spring", stiffness: 380, damping: 30 }}
            />
          )}
          <span className="relative">{option.label}</span>
        </button>
      ))}
    </div>
  );
}

const TH =
  "border-b border-[var(--edge)] bg-ink-900/40 text-xs font-medium tracking-[0.06em] text-ink-400 uppercase";

const TEXTAREA =
  "block w-full resize-y rounded-xl border border-ink-600/70 bg-ink-900/60 px-3.5 py-3 " +
  "font-mono text-sm leading-relaxed text-ink-100 placeholder:text-ink-500 transition-all duration-150 " +
  "hover:border-ink-500 focus:border-brand-400 focus:bg-ink-900 focus:ring-[3px] focus:ring-brand-500/15 focus:outline-none";

/* ----------------------------------------------------------------- page --- */

export function Admin() {
  const { user } = useAuth();
  const [tab, setTab] = useState<TabKey>("product");

  if (!user?.is_superuser) {
    return (
      <div className="space-y-6">
        <PageHeader
          eyebrow="Platform"
          title="Admin"
          subtitle="How the whole installation is doing, across every workspace."
        />
        <EmptyState icon={<ShieldIcon />} title="Staff only">
          {NOT_STAFF}
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Platform"
        title="Admin"
        subtitle="How the whole installation is doing, across every workspace."
      />

      <Switcher
        options={TABS}
        value={tab}
        onChange={setTab}
        layoutId="admin-tab"
      />

      {tab === "product" && <ProductTab />}
      {tab === "errors" && <ErrorsTab />}
      {tab === "workspaces" && <WorkspacesTab />}
      {tab === "pricing" && <PricingTab />}
    </div>
  );
}

/* -------------------------------------------------------------- product --- */

function ProductTab() {
  const [days, setDays] = useState<number>(7);
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setError(null);
    void api
      .get<Overview>(`/api/v1/admin/overview?days=${days}`)
      .then((result) => {
        if (live) setData(result);
      })
      .catch((err: unknown) => {
        if (!live) return;
        setError(failureText(err, "Could not load the platform overview."));
        setData(null);
      });
    return () => {
      live = false;
    };
  }, [days]);

  const byPlan = Object.entries(data?.workspaces.by_plan ?? {});

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-ink-400">
          Counted over the last {data?.window_days ?? days} days.
        </p>
        <Switcher
          options={WINDOWS.map((w) => ({ key: w, label: `${w}d` }))}
          value={days}
          onChange={setDays}
          layoutId="admin-window"
        />
      </div>

      {error && <Alert>{error}</Alert>}

      {!data && !error && <Loading label="Loading the platform overview" />}

      {data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <StatTile
              label="Workspaces"
              value={data.workspaces.total.toLocaleString()}
              hint={`${data.workspaces.new.toLocaleString()} created in this window`}
            />
            <StatTile
              label="Paying"
              value={data.workspaces.paying.toLocaleString()}
              tone={data.workspaces.paying > 0 ? "good" : undefined}
            />
            <StatTile
              label="Free"
              value={data.workspaces.free.toLocaleString()}
            />
            <StatTile
              label="People"
              value={data.people.total.toLocaleString()}
              hint={`${data.people.new.toLocaleString()} signed up in this window`}
            />
            <StatTile
              label="Unverified"
              value={data.people.unverified.toLocaleString()}
              tone={data.people.unverified > 0 ? "warn" : "good"}
              hint="Signed up but never confirmed their email."
            />
            <StatTile label="Flows" value={data.flows.total.toLocaleString()} />
            <StatTile
              label="Runs"
              value={data.runs.total.toLocaleString()}
              hint="In this window."
            />
            <StatTile
              label="Failure rate"
              value={formatPercent(data.runs.failure_rate, 1)}
              tone={
                data.runs.failure_rate >= 0.1
                  ? "bad"
                  : data.runs.failure_rate >= 0.02
                    ? "warn"
                    : "good"
              }
            />
            <StatTile
              label="Queued now"
              value={data.runs.queued_now.toLocaleString()}
              tone={data.runs.queued_now > 0 ? "warn" : "good"}
              hint="Waiting for a worker right now."
            />
          </div>

          {byPlan.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink-400">Workspaces by plan:</span>
              {byPlan.map(([code, count]) => (
                <Pill key={code}>
                  {code} {count.toLocaleString()}
                </Pill>
              ))}
            </div>
          )}

          <Card className="overflow-x-auto">
            <div className="border-b border-[var(--edge)] px-5 py-4">
              <h2 className="text-sm font-semibold text-ink-100">
                Slowest node types
              </h2>
              <p className="mt-1 text-sm text-ink-400">
                Average time per execution, across every workspace. This is
                where the platform spends its own compute.
              </p>
            </div>
            {data.slowest_nodes.length === 0 ? (
              <p className="px-5 py-8 text-center text-sm text-ink-500">
                Nothing has run in this window.
              </p>
            ) : (
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className={TH}>
                    <th scope="col" className="py-3 pr-4 pl-5">
                      Node type
                    </th>
                    <th scope="col" className="px-4 py-3 text-right">
                      Runs
                    </th>
                    <th scope="col" className="py-3 pr-5 pl-4 text-right">
                      Average
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.slowest_nodes.map((node) => (
                    <tr
                      key={node.node_type}
                      className="border-b border-[var(--edge)] last:border-b-0"
                    >
                      <td className="py-3 pr-4 pl-5 font-mono text-xs text-ink-200">
                        {node.node_type}
                      </td>
                      <td className="px-4 py-3 text-right text-ink-300">
                        {node.runs.toLocaleString()}
                      </td>
                      <td className="py-3 pr-5 pl-4 text-right text-ink-300">
                        {duration(node.average_ms)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- errors --- */

const ERROR_VIEWS = [
  { key: "grouped", label: "Grouped" },
  { key: "recent", label: "Latest" },
] as const;

type ErrorView = (typeof ERROR_VIEWS)[number]["key"];

function ErrorsTab() {
  const [view, setView] = useState<ErrorView>("grouped");
  const [grouped, setGrouped] = useState<GroupedError[] | null>(null);
  const [recent, setRecent] = useState<RecentError[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setError(null);
    const path = `/api/v1/admin/errors?days=7&limit=25&kind=${view}`;
    if (view === "grouped") {
      void api
        .get<ErrorFeed<GroupedError>>(path)
        .then((feed) => {
          if (live) setGrouped(feed.items);
        })
        .catch((err: unknown) => {
          if (!live) return;
          setError(failureText(err, "Could not load the grouped errors."));
          setGrouped(null);
        });
    } else {
      void api
        .get<ErrorFeed<RecentError>>(path)
        .then((feed) => {
          if (live) setRecent(feed.items);
        })
        .catch((err: unknown) => {
          if (!live) return;
          setError(failureText(err, "Could not load the latest errors."));
          setRecent(null);
        });
    }
    return () => {
      live = false;
    };
  }, [view]);

  const items = view === "grouped" ? grouped : recent;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-ink-400">
          Failures from the last 7 days, across every workspace.
        </p>
        <Switcher
          options={ERROR_VIEWS}
          value={view}
          onChange={setView}
          layoutId="admin-errors"
        />
      </div>

      {error && <Alert>{error}</Alert>}

      {!items && !error && <Loading label="Loading errors" />}

      {items && items.length === 0 && (
        <EmptyState icon={<ShieldIcon />} title="Nothing failed">
          No run has failed anywhere on the platform in the last 7 days.
        </EmptyState>
      )}

      {view === "grouped" && grouped && grouped.length > 0 && (
        <Card className="divide-y divide-[var(--edge)]">
          {grouped.map((item) => (
            <details
              key={`${item.node_type}:${item.signature}`}
              className="group px-5 py-4"
            >
              <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3">
                <Pill tone={item.count > 5 ? "bad" : "warn"}>
                  {item.count.toLocaleString()}
                </Pill>
                <span className="font-mono text-xs text-ink-400">
                  {item.node_type}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-ink-100">
                  {item.signature}
                </span>
                <span className="text-xs whitespace-nowrap text-ink-400">
                  Last seen <RelativeTime value={item.last_seen} />
                </span>
              </summary>
              <div className="mt-3 space-y-3">
                <pre className="overflow-x-auto rounded-xl border border-ink-700/60 bg-ink-900/60 px-4 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink-200">
                  {item.message}
                </pre>
                <Link
                  to={`/app/runs/${item.run_id}`}
                  className="inline-block text-sm text-brand-300 hover:underline"
                >
                  Open an example run
                </Link>
              </div>
            </details>
          ))}
        </Card>
      )}

      {view === "recent" && recent && recent.length > 0 && (
        <Card className="divide-y divide-[var(--edge)]">
          {recent.map((item) => (
            <div key={item.run_id} className="px-5 py-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink-100">
                  {item.workspace}
                </span>
                <span className="text-ink-600">/</span>
                <span className="text-sm text-ink-300">{item.flow}</span>
                <span className="ml-auto text-xs whitespace-nowrap text-ink-400">
                  <RelativeTime value={item.created_at} />
                </span>
              </div>
              <p className="mt-2 font-mono text-xs leading-relaxed wrap-anywhere text-ink-300">
                {item.error}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-ink-400">
                <span>Ran for {duration(item.duration_ms)}</span>
                <Link
                  to={`/app/runs/${item.run_id}`}
                  className="text-brand-300 hover:underline"
                >
                  Open the run
                </Link>
              </div>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}

/* ----------------------------------------------------------- workspaces --- */

function WorkspacesTab() {
  const [rows, setRows] = useState<AdminWorkspace[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void api
      .get<{ items: AdminWorkspace[] }>(
        "/api/v1/admin/workspaces?days=30&limit=50",
      )
      .then((result) => {
        if (live) setRows(result.items);
      })
      .catch((err: unknown) => {
        if (!live) return;
        setError(failureText(err, "Could not load the workspace list."));
        setRows(null);
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) return <Alert>{error}</Alert>;
  if (!rows) return <Loading label="Loading workspaces" />;
  if (rows.length === 0) {
    return (
      <EmptyState icon={<ShieldIcon />} title="No workspaces yet">
        Nobody has created a workspace on this installation.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-400">
        The 50 busiest workspaces, with run counts from the last 30 days.
      </p>
      <Card className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className={TH}>
              <th scope="col" className="py-3 pr-4 pl-5">
                Workspace
              </th>
              <th scope="col" className="px-4 py-3">
                Plan
              </th>
              <th scope="col" className="px-4 py-3">
                Status
              </th>
              <th scope="col" className="px-4 py-3 text-right">
                Members
              </th>
              <th scope="col" className="px-4 py-3 text-right">
                Runs
              </th>
              <th scope="col" className="py-3 pr-5 pl-4">
                Created
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.organization_id}
                className="border-b border-[var(--edge)] transition-colors last:border-b-0 hover:bg-ink-800/40"
              >
                <td className="py-3 pr-4 pl-5">
                  <p className="font-medium text-ink-100">{row.name}</p>
                  <p className="mt-0.5 font-mono text-xs text-ink-400">
                    {row.slug}
                  </p>
                </td>
                <td className="px-4 py-3">
                  <Pill tone={row.plan === "free" ? "neutral" : "info"}>
                    {row.plan}
                  </Pill>
                </td>
                <td className="px-4 py-3">
                  <Pill tone={row.is_active ? statusTone(row.status) : "bad"}>
                    {row.is_active ? row.status : "Suspended"}
                  </Pill>
                </td>
                <td className="px-4 py-3 text-right text-ink-300">
                  {row.members.toLocaleString()}
                </td>
                <td className="px-4 py-3 text-right text-ink-300">
                  {row.runs.toLocaleString()}
                </td>
                <td className="py-3 pr-5 pl-4 whitespace-nowrap text-ink-300">
                  <RelativeTime value={row.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------- pricing --- */

const NUMERIC_FIELDS = [
  { key: "runs_per_month", label: "Runs per month" },
  { key: "flows", label: "Flows" },
  { key: "seats", label: "Seats" },
  { key: "history_days", label: "History days" },
] as const;

type NumericKey = (typeof NUMERIC_FIELDS)[number]["key"];

type PlanDialog =
  | { kind: "edit"; plan: Plan }
  | { kind: "reset"; plan: Plan }
  | null;

function PricingTab() {
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<PlanDialog>(null);

  const load = useCallback(async () => {
    try {
      setPlans(await api.get<Plan[]>("/api/v1/admin/plans"));
      setError(null);
    } catch (err) {
      setError(failureText(err, "Could not load the plans."));
      setPlans(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <Alert>{error}</Alert>;
  if (!plans) return <Loading label="Loading plans" />;

  return (
    <div className="space-y-6">
      <Alert tone="info">
        The price here is what the console displays. A customer is charged
        whatever the payment provider's product costs, so changing a price
        needs a new product id as well.
      </Alert>

      <div className="grid gap-4 lg:grid-cols-3">
        {plans.map((plan) => (
          <PlanCard
            key={plan.code}
            plan={plan}
            onEdit={() => setDialog({ kind: "edit", plan })}
            onReset={() => setDialog({ kind: "reset", plan })}
          />
        ))}
      </div>

      {dialog?.kind === "edit" && (
        <EditPlanDialog
          plan={dialog.plan}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            void load();
          }}
        />
      )}
      {dialog?.kind === "reset" && (
        <ResetPlanDialog
          plan={dialog.plan}
          onClose={() => setDialog(null)}
          onReset={() => {
            setDialog(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

function PlanCard({
  plan,
  onEdit,
  onReset,
}: {
  plan: Plan;
  onEdit: () => void;
  onReset: () => void;
}) {
  const rows: { key: string; label: string; value: string }[] = [
    { key: "price_inr", label: "Price in rupees", value: priceText(plan.price_inr) },
    { key: "price_usd", label: "Price in dollars", value: priceText(plan.price_usd) },
    {
      key: "runs_per_month",
      label: "Runs per month",
      value: limitText(plan.runs_per_month),
    },
    { key: "flows", label: "Flows", value: limitText(plan.flows) },
    { key: "seats", label: "Seats", value: limitText(plan.seats) },
    {
      key: "history_days",
      label: "History days",
      value: limitText(plan.history_days),
    },
  ];

  return (
    <Card className="flex flex-col p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold text-ink-100">
              {plan.name}
            </h2>
            <Pill>{plan.code}</Pill>
            {plan.overridden.length > 0 && <Pill tone="warn">Edited</Pill>}
          </div>
          <p className="mt-1 text-sm leading-relaxed text-ink-400">
            {plan.tagline}
          </p>
        </div>
        <div className="flex flex-none gap-2">
          <Button variant="secondary" onClick={onEdit}>
            Edit
          </Button>
          <Button variant="ghost" onClick={onReset}>
            Reset to defaults
          </Button>
        </div>
      </div>

      <dl className="mt-5 grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {rows.map((row) => (
          <div
            key={row.key}
            className="flex items-center justify-between gap-3 border-b border-[var(--edge)] py-1.5 last:border-b-0"
          >
            <dt className="text-sm text-ink-400">{row.label}</dt>
            <dd className="flex items-center gap-2 text-sm text-ink-100">
              {plan.overridden.includes(row.key) && (
                <Pill tone="warn">Edited</Pill>
              )}
              {row.value}
            </dd>
          </div>
        ))}
      </dl>

      {plan.features.length > 0 && (
        <ul className="mt-4 space-y-1.5">
          {plan.features.map((feature) => (
            <li
              key={feature}
              className="flex items-start gap-2 text-sm text-ink-300"
            >
              <span className="mt-1.5 h-1.5 w-1.5 flex-none rounded-full bg-brand-400" />
              <span className="min-w-0">{feature}</span>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-4 font-mono text-xs wrap-anywhere text-ink-400">
        Product id: {plan.product_id || "not set"}
      </p>
    </Card>
  );
}

function EditPlanDialog({
  plan,
  onClose,
  onSaved,
}: {
  plan: Plan;
  onClose: () => void;
  onSaved: () => void;
}) {
  const formId = useId();
  const [name, setName] = useState(plan.name);
  const [tagline, setTagline] = useState(plan.tagline);
  const [productId, setProductId] = useState(plan.product_id || "");
  const [features, setFeatures] = useState(plan.features.join("\n"));
  const [priceInr, setPriceInr] = useState(plan.price_inr);
  const [priceUsd, setPriceUsd] = useState(plan.price_usd);
  const [numbers, setNumbers] = useState<Record<NumericKey, string>>({
    runs_per_month: "",
    flows: "",
    seats: "",
    history_days: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const current: Record<NumericKey, number | null> = {
    runs_per_month: plan.runs_per_month,
    flows: plan.flows,
    seats: plan.seats,
    history_days: plan.history_days,
  };

  async function submit(event: FormEvent) {
    event.preventDefault();

    // Only what actually changed is sent. Sending every field on every save
    // would mark a plan as edited for values nobody touched, and the "Edited"
    // marks are how staff see at a glance what is no longer the default.
    const body: Record<string, unknown> = {};
    const reset: string[] = [];
    const nextFeatures = features
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);

    for (const [key, next, now] of [
      ["name", name.trim(), plan.name],
      ["tagline", tagline.trim(), plan.tagline],
      ["price_inr", priceInr.trim(), plan.price_inr],
      ["price_usd", priceUsd.trim(), plan.price_usd],
    ] as const) {
      if (next === now) continue;
      if (next === "") reset.push(key);
      else body[key] = next;
    }

    if (nextFeatures.join("\n") !== plan.features.join("\n")) {
      if (nextFeatures.length === 0) reset.push("features");
      else body.features = nextFeatures;
    }

    const nextProduct = productId.trim();
    if (nextProduct !== (plan.product_id || "")) {
      body.product_id = nextProduct || null;
    }

    for (const field of NUMERIC_FIELDS) {
      const raw = numbers[field.key].trim();
      if (raw === "") continue;
      const parsed = Number(raw);
      if (!Number.isInteger(parsed)) {
        setError(`${field.label} must be a whole number, or use -1 for unlimited.`);
        return;
      }
      if (parsed !== current[field.key]) body[field.key] = parsed;
    }

    if (reset.length) body.reset = reset;
    if (Object.keys(body).length === 0) {
      // Nothing to write. Saying so beats a request that changes nothing and
      // looks, from the outside, exactly like a save that failed silently.
      onClose();
      return;
    }

    setBusy(true);
    setError(null);
    try {
      await api.patch<Plan>(`/api/v1/admin/plans/${plan.code}`, body);
      onSaved();
    } catch (err) {
      setError(failureText(err, "Could not save this plan."));
      setBusy(false);
    }
  }

  return (
    <Modal
      size="lg"
      title={`Edit ${plan.name}`}
      description="What the pricing page and the console show for this plan."
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form={formId} loading={busy}>
            Save plan
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="space-y-5" noValidate>
        {error && <Alert>{error}</Alert>}

        <Field
          label="Name"
          value={name}
          autoFocus
          onChange={(event) => setName(event.target.value)}
        />
        <Field
          label="Tagline"
          value={tagline}
          hint="One line, shown under the plan name."
          onChange={(event) => setTagline(event.target.value)}
        />

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Price in rupees"
            value={priceInr}
            hint="Written the way a customer should read it, such as ₹2,499."
            onChange={(event) => setPriceInr(event.target.value)}
          />
          <Field
            label="Price in dollars"
            value={priceUsd}
            hint="Such as $29. Shown to customers paying outside India."
            onChange={(event) => setPriceUsd(event.target.value)}
          />
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          {NUMERIC_FIELDS.map((field) => (
            <Field
              key={field.key}
              label={field.label}
              inputMode="numeric"
              placeholder={
                current[field.key] === null
                  ? "Unlimited"
                  : String(current[field.key])
              }
              value={numbers[field.key]}
              onChange={(event) =>
                setNumbers((previous) => ({
                  ...previous,
                  [field.key]: event.target.value.replace(/[^\d-]/g, ""),
                }))
              }
            />
          ))}
        </div>
        <p className="text-sm text-ink-400">
          Leave a number empty to keep what the plan has now. Type -1 for
          unlimited.
        </p>

        <div className="space-y-1.5">
          <label
            htmlFor={`${formId}-features`}
            className="block text-sm font-medium text-ink-200"
          >
            Features
          </label>
          <textarea
            id={`${formId}-features`}
            value={features}
            spellCheck={false}
            rows={6}
            onChange={(event) => setFeatures(event.target.value)}
            className={TEXTAREA}
          />
          <p className="text-sm text-ink-400">
            One per line. These are the bullet points on the pricing page.
          </p>
        </div>

        <Field
          label="Provider product id"
          value={productId}
          placeholder="Not set"
          hint="The product the payment provider charges against. A new price needs a new product."
          onChange={(event) => setProductId(event.target.value)}
        />
      </form>
    </Modal>
  );
}

/** Field names as a person reads them, for the list of what is edited. */
const FIELD_LABELS: Record<string, string> = {
  name: "Name",
  tagline: "Tagline",
  price_inr: "Price in rupees",
  price_usd: "Price in dollars",
  runs_per_month: "Runs per month",
  flows: "Flows",
  seats: "Seats",
  history_days: "History days",
  features: "Features",
  product_id: "Product id",
};

function ResetPlanDialog({
  plan,
  onClose,
  onReset,
}: {
  plan: Plan;
  onClose: () => void;
  onReset: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function reset() {
    setBusy(true);
    setError(null);
    try {
      await api.del<Plan>(`/api/v1/admin/plans/${plan.code}`);
      onReset();
    } catch (err) {
      setError(failureText(err, "Could not reset this plan."));
      setBusy(false);
    }
  }

  return (
    <Modal
      size="sm"
      title={`Reset ${plan.name}`}
      description="Every edit to this plan goes back to the value it ships with."
      onClose={() => {
        if (!busy) onClose();
      }}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={() => void reset()}
            loading={busy}
            className="hover:brightness-110"
            style={{
              background: "var(--status-bad)",
              color: "var(--color-ink-950)",
            }}
          >
            Reset plan
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <p className="text-sm leading-relaxed text-ink-300">
          The prices, limits, features and product id you set here are dropped,
          and the values it ships with take over straight away. Anyone looking at
          the pricing page sees the change on their next load. This cannot be
          undone.
        </p>
        {plan.overridden.length > 0 && (
          <ul className="flex flex-wrap gap-1.5">
            {plan.overridden.map((field) => (
              <li key={field}>
                <Pill tone="warn">{FIELD_LABELS[field] ?? field}</Pill>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------- glyph --- */

function ShieldIcon(): ReactNode {
  return (
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
  );
}
