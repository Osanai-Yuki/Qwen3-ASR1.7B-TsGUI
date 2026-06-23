import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Dev-mode proxy: forward /api to the FastAPI backend so the UI can be
    // developed with hot reload while the API runs on port 8000.
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    // Backend mounts `frontend/dist` as static files (main.py StaticFiles),
    // so the build output must land here.
    outDir: "dist",
  },
});
