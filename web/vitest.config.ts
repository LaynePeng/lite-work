import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 前端组件测试（P2-6）：jsdom 环境 + Testing Library。
// 与 vite.config.ts 分离——构建配置保持零测试依赖。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
