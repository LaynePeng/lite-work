// Sync npm-side version fields from the single source of truth: litework/__init__.py __version__.
// Updates the top-level "version" in package.json, web/package.json and both lockfiles.
// Runs automatically at the start of every build; also guarded by tests/test_version_sync.py.
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function readVersion() {
  const init = fs.readFileSync(path.join(root, "litework", "__init__.py"), "utf-8");
  const m = init.match(/^__version__\s*=\s*["']([^"']+)["']/m);
  if (!m) {
    console.error("[sync-version] __version__ not found in litework/__init__.py");
    process.exit(1);
  }
  return m[1];
}

function updateJsonVersion(file, version) {
  const full = path.join(root, file);
  if (!fs.existsSync(full)) return false;
  const raw = fs.readFileSync(full, "utf-8");
  const data = JSON.parse(raw);
  let changed = data.version !== version;
  data.version = version;
  // npm lockfile v3 会在 packages[""] 中重复记录项目自身版本。
  if (data.packages?.[""] && data.packages[""].version !== version) {
    data.packages[""].version = version;
    changed = true;
  }
  if (!changed) return false;
  fs.writeFileSync(full, JSON.stringify(data, null, 2) + "\n");
  return true;
}

const version = readVersion();
const targets = ["package.json", "web/package.json", "package-lock.json", "web/package-lock.json"];
let changed = 0;
for (const t of targets) {
  if (updateJsonVersion(t, version)) {
    console.log(`[sync-version] ${t}: -> ${version}`);
    changed += 1;
  }
}
if (changed === 0) console.log(`[sync-version] all in sync at ${version}`);

// Exported for programmatic use in package.mjs
export { readVersion, version };