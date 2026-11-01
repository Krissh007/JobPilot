import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server on this computer only; /api is proxied to the FastAPI backend.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // changeOrigin: the API accepts only its own Host (127.0.0.1:8000); run it with JOBPILOT_DEV=1 so it accepts this origin too.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
