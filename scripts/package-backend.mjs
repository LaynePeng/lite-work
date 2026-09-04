// Package the backend: PyInstaller produces a standalone binary (release/backend/lite-work-backend)
// Cross-platform: adapts venv paths and the --add-data separator for Windows / macOS / Linux.
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const isWindows = process.platform === "win32";
const python = isWindows
  ? path.join(root, ".venv", "Scripts", "python.exe")
  : path.join(root, ".venv", "bin", "python");
const outDir = path.join(root, "release", "backend");
const clean = process.argv.includes("--clean") || process.env.LITEWORK_PYINSTALLER_CLEAN === "1";

if (!fs.existsSync(python)) {
  console.error(
    isWindows
      ? "[package] .venv\\Scripts\\python.exe not found. Run first: python -m venv .venv && .venv\\Scripts\\pip install -e ."
      : "[package] .venv/bin/python not found. Run first: python3 -m venv .venv && .venv/bin/pip install -e ."
  );
  process.exit(1);
}

fs.mkdirSync(outDir, { recursive: true });
// 只清理最终 onedir 输出，保留 release/_build 的分析缓存。
// 否则 PyInstaller 增量收集时可能与旧的 Python.framework 链接冲突。
fs.rmSync(path.join(outDir, "lite-work-backend"), { recursive: true, force: true });

const webDist = path.join(root, "web", "dist");
if (!fs.existsSync(path.join(webDist, "index.html"))) {
  console.error("[package] web/dist not found. Run first: npm run build:web");
  process.exit(1);
}

// Windows uses ';', macOS/Linux use ':'
const sep = isWindows ? ";" : ":";

const specArgs = [
  "--noconfirm",
  ...(clean ? ["--clean"] : []),
  "--onedir",
  "--name", "lite-work-backend",
  "--distpath", outDir,
  "--workpath", path.join(root, "release", "_build"),
  "--specpath", path.join(root, "release", "_spec"),
  "--paths", root,
  "--add-data", `${webDist}${sep}web${path.sep}dist`,
  "--add-data", `${path.join(root, "skills")}${sep}skills`,
  "--collect-all", "fastapi",
  "--collect-all", "uvicorn",
  "--collect-all", "starlette",
  "--collect-submodules", "httpx",
  "--collect-all", "tree_sitter",
  "--collect-all", "tree_sitter_typescript",
  "--collect-all", "tree_sitter_java",
  "--collect-all", "tree_sitter_go",
  "--collect-submodules", "pathspec",
  "--collect-submodules", "litework",
  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols",
  "--hidden-import", "uvicorn.protocols.http",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "uvicorn.lifespan",
  "--hidden-import", "uvicorn.lifespan.on",
  path.join(root, "litework_entry.py"),
];

console.log("[package] Packaging backend with PyInstaller...");
console.log(`  python: ${python}`);
console.log(`  output: ${outDir}/lite-work-backend/ (--onedir, faster than onefile)`);
console.log(`  mode: --onedir${clean ? " --clean" : " (incremental cache)"}`);
const startedAt = Date.now();
execSync([python, "-m", "PyInstaller", ...specArgs].join(" "), {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: root },
});
console.log(`  elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);

const outDirName = isWindows ? "lite-work-backend.exe" : "lite-work-backend";
const binary = path.join(outDir, "lite-work-backend", outDirName);
if (!fs.existsSync(binary)) {
  console.error("[package] Backend packaging failed: binary was not generated");
  process.exit(1);
}
console.log(`[package] Backend binary: ${binary}`);
