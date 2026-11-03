// 根依赖装完后顺带装 web 前端依赖：`npm install` 一条命令就够（开发体验）。
//
// CI 里**必须跳过**：那等于在 npm 自己的生命周期钩子里再起一个 npm 进程，
// npm 10 偶发 "Exit handler never called!"（npm/cli 已知问题）直接把 job 打死。
// v2.1.1 的 macOS release 就是这么挂的——同一份配置在 v2.1.0 却是通过的，
// 属于「不修就随机复发」。CI 工作流本来就显式跑 `npm --prefix web ci`，
// 所以跳过这里不损失任何东西，还省掉一次重复安装。
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

/**
 * 是否该在这里装 web 依赖。
 *
 * GitHub Actions 默认注入 `CI=true`；只把它当「非空且不是 0/false」才认，
 * 免得用户手工 `CI=false npm install` 时行为反直觉。
 */
export function shouldInstallWeb(env = process.env) {
  const ci = String(env.CI ?? "").trim().toLowerCase();
  return !(ci && ci !== "0" && ci !== "false");
}

function main() {
  if (!shouldInstallWeb()) {
    console.log("[postinstall] CI 环境：跳过 web 依赖（工作流会显式执行 npm --prefix web ci）");
    return 0;
  }
  // 用 spawn 而不是 execSync：让 npm 的进度/告警直接透到当前终端。
  const child = spawn("npm", ["install", "--prefix", "web"], {
    cwd: root,
    stdio: "inherit",
    // Windows 上 npm 是 npm.cmd，必须走 shell 才能解析
    shell: process.platform === "win32",
  });
  return new Promise((resolve) => {
    child.on("error", (err) => {
      console.error(`[postinstall] 无法启动 npm: ${err.message}`);
      resolve(1);
    });
    child.on("exit", (code) => resolve(code ?? 0));
  });
}

// 直接执行时才跑 main（被测试 import 时不得有副作用）
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const code = await main();
  process.exit(code);
}
