import base from "./vite.config"
import { defineConfig, mergeConfig } from "vite"
export default mergeConfig(base, defineConfig({
  server: {
    port: 5199, host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8899",
      "/download": "http://127.0.0.1:8899",
      "/proxy": "http://127.0.0.1:8899",
    },
  },
}))
