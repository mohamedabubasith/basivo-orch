import { Link } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { Badge, Button, Card } from "../../components/ui";

export function Dashboard() {
  const { user } = useAuth();

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-100">
          Welcome{user?.email ? `, ${user.email.split("@")[0]}` : ""}
        </h1>
        <p className="mt-1.5 text-ink-400">
          Your account is ready. The pipeline builder lands in the next beta drop.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card className="p-5">
          <p className="text-sm text-ink-400">Account</p>
          <p className="mt-1.5 truncate text-lg font-medium text-ink-100">{user?.email}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Badge className={user?.is_verified ? "text-ok-500" : "text-warn-500"}>
              {user?.is_verified ? "Email confirmed" : "Email unconfirmed"}
            </Badge>
            <Badge className={user?.totp_enabled ? "text-ok-500" : "text-ink-400"}>
              {user?.totp_enabled ? "2FA on" : "2FA off"}
            </Badge>
          </div>
        </Card>

        <Card className="p-5">
          <p className="text-sm text-ink-400">Pipelines</p>
          <p className="mt-1.5 text-lg font-medium text-ink-100">0</p>
          <p className="mt-3 text-sm text-ink-500">Nothing built yet.</p>
        </Card>

        <Card className="p-5">
          <p className="text-sm text-ink-400">Runs this month</p>
          <p className="mt-1.5 text-lg font-medium text-ink-100">0</p>
          <p className="mt-3 text-sm text-ink-500">Run history appears here.</p>
        </Card>
      </div>

      {!user?.totp_enabled && (
        <Card className="flex flex-col items-start justify-between gap-4 p-5 sm:flex-row sm:items-center">
          <div>
            <p className="font-medium text-ink-100">Add two-factor authentication</p>
            <p className="mt-1 text-sm text-ink-400">
              Pipelines hold API keys and can act on your systems. A second factor is worth the
              thirty seconds.
            </p>
          </div>
          <Link to="/app/security" className="flex-none">
            <Button>Set up 2FA</Button>
          </Link>
        </Card>
      )}

      <Card className="p-8 text-center">
        <p className="text-ink-300">The pipeline canvas is not in this beta yet.</p>
        <p className="mt-1.5 text-sm text-ink-500">
          Authentication, workspaces and the run-log foundation shipped first, on purpose — they
          are the parts that are painful to retrofit.
        </p>
      </Card>
    </div>
  );
}
