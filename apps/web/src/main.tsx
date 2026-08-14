import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Bundled, not fetched from a CDN: index.css has named "Inter var" since day
// one without ever loading it, so every user has been reading the system
// fallback. Self-hosting through the bundle also keeps the app working behind
// strict CSPs and offline dev.
import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";

import App from "./App.tsx";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
