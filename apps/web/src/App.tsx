import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./lib/auth";
import { ThemeProvider } from "./lib/theme";
import { Landing } from "./routes/Landing";
import { ApiKeys } from "./routes/app/ApiKeys";
import { AppShell, RequireAuth, RequireVerified } from "./routes/app/AppShell";
import { Builder } from "./routes/app/Builder";
import { Dashboard } from "./routes/app/Dashboard";
import { EmailGate } from "./routes/app/EmailGate";
import { Flows } from "./routes/app/Flows";
import { Runs } from "./routes/app/Runs";
import { Security } from "./routes/app/Security";
import { ForgotPassword } from "./routes/auth/ForgotPassword";
import { Login } from "./routes/auth/Login";
import { Register } from "./routes/auth/Register";
import { ResetPassword } from "./routes/auth/ResetPassword";
import { TwoFactor } from "./routes/auth/TwoFactor";
import { VerifyEmail } from "./routes/auth/VerifyEmail";

/**
 * The route table. Deliberately *not* wrapped in a keyed <AnimatePresence>.
 *
 * It used to be: one `motion.div key={location.pathname}` around this entire
 * tree. That cross-faded navigation, and it also unmounted and rebuilt
 * everything on every click — the shell, the sidebar, the workspace provider
 * and its `/orgs` request. Switching tabs looked like a full page refresh
 * because structurally it was one.
 *
 * Page transitions belong *inside* whatever should survive them, so the
 * animation now lives around the shell's <Outlet /> and the chrome stays put.
 */
function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />

      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/two-factor" element={<TwoFactor />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      {/* These two paths are fixed by the API: it emails
          `{FRONTEND_BASE_URL}/auth/verify?token=…` and
          `/auth/reset-password?token=…`. They must match
          `_frontend_link()` in apps/api/basivo_orch/auth/email/sender.py,
          and renaming one breaks every link already sitting in an inbox. */}
      <Route path="/auth/verify" element={<VerifyEmail />} />
      <Route path="/auth/reset-password" element={<ResetPassword />} />

      <Route element={<RequireAuth />}>
        {/* Signed in, but not yet through the email gate. Outside the
            shell on purpose: there is no workspace to put chrome around
            until the address is confirmed. */}
        <Route path="/confirm-email" element={<EmailGate />} />

        <Route element={<RequireVerified />}>
          {/* Outside <AppShell> on purpose: the canvas takes the whole
              viewport rather than sitting in the same max-width column as a
              settings form, with the sidebar eating the axis a graph needs
              most. It carries its own way back. */}
          <Route path="/app/flows/:flowId" element={<Builder />} />

          <Route path="/app" element={<AppShell />}>
            <Route index element={<Dashboard />} />
            <Route path="flows" element={<Flows />} />
            <Route path="runs" element={<Runs />} />
            <Route path="api-keys" element={<ApiKeys />} />
            <Route path="security" element={<Security />} />
          </Route>
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}
