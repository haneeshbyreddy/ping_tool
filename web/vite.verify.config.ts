// THROWAWAY — browser verification only. Serves the SPA from source on :5199
// and proxies the API to the second central on :8899, so verifying never writes
// to src/wisp/central/static/ (which IS the live deploy). Delete after the run.
import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5199,
    proxy: {
      "/api": "http://127.0.0.1:8899",
      "/download": "http://127.0.0.1:8899",
      "/proxy": "http://127.0.0.1:8899",
    },
  },
})
