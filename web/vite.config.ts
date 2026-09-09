import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式下 /api 代理到本地 Core（与 Electron dev 模式共用）。
// P0-2 起 Core 默认开启 Bearer 鉴权，开发联调二选一：
//   1) LITEWORK_CORE_TOKEN=<token> npm run dev   （Core 用 --token <token> 启动）
//   2) Core 用 --no-token 启动（仅本机调试，不设置该环境变量）
const coreToken = process.env.LITEWORK_CORE_TOKEN || "";

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
        headers: coreToken ? { Authorization: `Bearer ${coreToken}` } : {},
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
  },
});
