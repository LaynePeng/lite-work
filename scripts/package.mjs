// Full packaging: PyInstaller backend -> electron-builder produces .app + DMG
// Notes:
// - DMG 用 electron-builder 内置 target（与 markdown-viewer 同方案）：自带成熟布局
//   （大图标 + Applications 拖入位 + 背景箭头），无需手写 .DS_Store。
// - 产线校验：构建后挂载 DMG 检查 .app 完整性（历史版本曾出现 DMG 内空 .app，
//   被 macOS 判为损坏——这里挂载验证兜底）。
// - On restricted networks (CN), set mirror env vars, otherwise electron-builder hangs while downloading binaries.
//   Usage: ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/" \
//          ELECTRON_BUILDER_BINARIES_MIRROR="https://npmmirror.com/mirrors/electron-builder-binaries/" \
//          node scripts/package.mjs
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf-8"));
let version = pkg.version;

const env = { ...process.env };

// CN users can preset ELECTRON_MIRROR etc. to avoid electron-builder download hangs.
// Provide a default mirror fallback (only when unset); never override existing env vars.
if (!env.ELECTRON_MIRROR && !env.ELECTRON_BUILDER_BINARIES_MIRROR) {
  env.ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/";
  env.ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/";
}

function step(label, fn) {
  const start = Date.now();
  process.stdout.write(`[${start % 100000}] ${label}... `);
  fn();
  console.log(`(${((Date.now() - start) / 1000).toFixed(1)}s)`);
}

// Version single source of truth: sync npm-side version fields from litework/__version__
step("sync-version", () => execSync("node scripts/sync-version.mjs", { cwd: root, stdio: "inherit" }));
// Re-read after sync so the DMG name follows the single source of truth
const syncedPkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf-8"));
version = syncedPkg.version;

console.log("[package] Step 0/4: Generate app icon");
step("icon", () => execSync("node scripts/generate-icon.mjs", { cwd: root, stdio: "inherit" }));

console.log("[package] Step 1/4: Package backend binary");
const backendCleanArg = process.argv.includes("--clean") ? " --clean" : "";
step(`PyInstaller --onedir${backendCleanArg}`, () =>
  execSync(`node scripts/package-backend.mjs${backendCleanArg}`, { cwd: root, stdio: "inherit" })
);

console.log("[package] Step 2/4: electron-builder produces .app + DMG");
step("electron-builder", () => execSync("npx electron-builder --mac dmg --publish never", { cwd: root, stdio: "inherit", env }));

const appDir = path.join(root, "release", "mac-arm64", "lite-work.app");
if (!fs.existsSync(path.join(appDir, "Contents", "MacOS", "lite-work"))) {
  console.error("[package] Generated .app is incomplete. Check the electron-builder output");
  process.exit(1);
}

const dmgName = `lite-work-${version}-arm64.dmg`;
const dmgPath = path.join(root, "release", dmgName);
if (!fs.existsSync(dmgPath)) {
  // electron-builder 命名口径变化兜底：找 release 下最新的 dmg 改名为标准名
  const cand = fs.readdirSync(path.join(root, "release"))
    .filter((f) => f.endsWith(".dmg"))
    .map((f) => ({ f, m: fs.statSync(path.join(root, "release", f)).mtimeMs }))
    .sort((a, b) => b.m - a.m)[0];
  if (!cand) {
    console.error("[package] electron-builder 未产出 DMG");
    process.exit(1);
  }
  fs.renameSync(path.join(root, "release", cand.f), dmgPath);
}

console.log("[package] Step 3/4: Verify DMG contents (mount + check .app)");
{
  const mountPoint = path.join(os.tmpdir(), `lc-dmg-verify-${Date.now()}`);
  fs.mkdirSync(mountPoint, { recursive: true });
  try {
    execSync(`hdiutil attach "${dmgPath}" -nobrowse -readonly -mountpoint "${mountPoint}" -quiet`, { stdio: "inherit" });
    const mountedApp = path.join(mountPoint, "lite-work.app");
    if (!fs.existsSync(path.join(mountedApp, "Contents", "MacOS", "lite-work"))) {
      console.error("[package] DMG 内 .app 不完整（历史空 .app 问题）——中止发布");
      process.exit(1);
    }
    const appsLink = fs.existsSync(path.join(mountPoint, "Applications"));
    console.log(`[package] DMG 校验通过（.app 完整${appsLink ? "，含 Applications 链接" : ""}）`);
  } finally {
    try { execSync(`hdiutil detach "${mountPoint}" -quiet`, { stdio: "ignore" }); } catch { /* ignore */ }
    try { fs.rmSync(mountPoint, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

console.log(`[package] Done -> release/${dmgName}`);
