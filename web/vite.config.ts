import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式下 /api 代理到本地 Core（与 Electron dev 模式共用）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.LITEWORK_CORE_URL || "http://127.0.0.1:8787",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
  },
});