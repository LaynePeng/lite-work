// Cross-platform Electron launcher (dev mode): inject LITEWORK_DEV_URL pointing to the Vite dev server
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// The string exported by the electron package is the real binary path (dist/electron.exe on Windows)
const require = createRequire(import.meta.url);
const electronPath = require("electron");
const child = spawn(electronPath, ["."], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, LITEWORK_DEV_URL: "http://localhost:5173" },
});
child.on("exit", (code) => process.exit(code ?? 0));