import path from "node:path"
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Central's server (src/wisp/central/server.py) serves whatever's in
// src/wisp/central/static/ directly, with no build step at request time —
// so `npm run build` has to drop its output exactly there. See CLAUDE.md.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../src/wisp/central/static"),
    emptyOutDir: true,
  },
})
