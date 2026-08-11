import { AnimatePresence, motion } from "motion/react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { AuthProvider } from "./lib/auth";
import { Landing } from "./routes/Landing";
import { AppShell, RequireAuth } from "./routes/app/AppShell";
import { Dashboard } from "./routes/app/Dashboard";
import { Security } from "./routes/app/Security";
import { ForgotPassword } from "./routes/auth/ForgotPassword";
import { Login } from "./routes/auth/Login";
import { Register } from "./routes/auth/Register";
import { ResetPassword } from "./routes/auth/ResetPassword";
import { TwoFactor } from "./routes/auth/TwoFactor";
import { VerifyEmail } from "./routes/auth/VerifyEmail";

/** Cross-fades between routes so navigation is not a hard cut. */
function AnimatedRoutes() {
  const location = useLocation();
  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={location.pathname}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.18 }}
      >
        <Routes location={location}>
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
            <Route path="/app" element={<AppShell />}>
              <Route index element={<Dashboard />} />
              <Route path="security" element={<Security />} />
            </Route>
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </motion.div>
    </AnimatePresence>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AnimatedRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
