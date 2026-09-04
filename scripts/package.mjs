// Full packaging: PyInstaller backend -> electron-builder --dir produces .app -> hdiutil manually builds the DMG
// Notes:
// - electron-builder producing the DMG directly can yield an empty .app (flagged by macOS as damaged/malware),
//   so we do it in two steps: generate the .app first (--dir), then wrap it with hdiutil create.
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

console.log("[package] Step 2/4: electron-builder --dir produces .app");
step("electron-builder", () => execSync("npx electron-builder --mac --dir --publish never", { cwd: root, stdio: "inherit", env }));

const appDir = path.join(root, "release", "mac-arm64", "lite-work.app");
if (!fs.existsSync(path.join(appDir, "Contents", "MacOS", "lite-work"))) {
  console.error("[package] Generated .app is incomplete. Check the electron-builder output");
  process.exit(1);
}

console.log("[package] Step 3/4: Wrap DMG with hdiutil");
const staging = fs.mkdtempSync(path.join(os.tmpdir(), "lc-dmg-"));
const dmgName = `lite-work-${version}-arm64.dmg`;
const dmgPath = path.join(root, "release", dmgName);

try {
  fs.copyFileSync(path.join(appDir, "Contents", "Resources", "app-icon.icns"),
    path.join(staging, ".VolumeIcon.icns"));
} catch { /* icon is optional */ }
step("prepare", () => execSync(`cp -R "${appDir}" "${staging}/lite-work.app"`, { cwd: root, stdio: "inherit" }));
fs.symlinkSync("/Applications", path.join(staging, "Applications"), "dir");

step("hdiutil create", () => execSync(
  `hdiutil create -volname "lite-work ${version}" -srcfolder "${staging}" -ov -format UDZO "${dmgPath}"`,
  { cwd: root, stdio: "inherit" }
));

fs.rmSync(staging, { recursive: true, force: true });
console.log(`[package] Done -> release/${dmgName}`);
