import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Relative, because a build is served from /p/<token>/v<n>/ and from
  // /p/<token>/ at the same time. An absolute base would work at one of those
  // and 404 at the other.
  base: "./",
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
