import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В compose api доступен по имени сервиса; на хосте — на localhost:8000.
const target = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: true, port: 5173, proxy: { "/api": target, "/health": target } },
});
