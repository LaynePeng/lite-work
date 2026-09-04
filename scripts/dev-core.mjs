// Cross-platform local Core launcher (dev mode): resolve python inside venv
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const isWindows = process.platform === "win32";
const python = isWindows
  ? path.join(root, ".venv", "Scripts", "python.exe")
  : path.join(root, ".venv", "bin", "python");

if (!fs.existsSync(python)) {
  console.error(`[dev] venv Python not found: ${python}`);
  console.error("[dev] Run first: python -m venv .venv && .venv/Scripts/pip install -e .");
  process.exit(1);
}

const args = ["-m", "litework", "serve", "--port", "8787", "--log-level", "info"];
console.log(`[dev] ${python} ${args.join(" ")}`);
const child = spawn(python, args, { cwd: root, stdio: "inherit" });
child.on("exit", (code) => process.exit(code ?? 0));