import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// No API proxy, deliberately.
//
// The obvious setup — proxy /auth to the API so the browser sees one origin —
// cannot work here. The API's emails link the *browser* to
// `{FRONTEND_BASE_URL}/auth/verify?token=…` and `/auth/reset-password?token=…`,
// while the API itself serves `POST /auth/verify` and `POST /auth/reset-password`.
// Proxying /auth would send those page loads to the API and the user would get
// JSON instead of a form.
//
// So the SPA and the API keep separate origins, which is what the auth package
// assumes anyway (FRONTEND_BASE_URL and PUBLIC_BASE_URL are separate settings).
// Cookies still work: localhost:5173 and localhost:8000 differ only by port, and
// ports do not affect same-site, so SameSite=lax cookies are sent normally.
//
// The same holds in production with app.example.com / api.example.com, provided
// COOKIE_DOMAIN=.example.com is set so both share the cookie jar. See the README.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
  preview: { port: 4173 },
  build: { sourcemap: true },
});
