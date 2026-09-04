// lite-work Electron 主进程
// 职责：
//   1. 读取客户端配置（~/.lite-work/client.json），支持远程 Core 直连
//   2. 无配置时自动拉起本地 Python Core（打包后使用内置二进制）
//   3. 等待 LITEWORK_CORE_READY 就绪标记后打开窗口
//   4. 「打开项目」：通过当前 Core 热切换 workspace，不新建或重启后端
//   5. 退出时回收后端进程
//
// 注意：这里没有调用 app.requestSingleInstanceLock()。每次从系统启动应用都会
// 创建独立的 Electron 进程与本地 Core，可分别绑定不同项目。
const { app, BrowserWindow, Menu, session, shell, dialog, ipcMain } = require("electron");
const { spawn } = require("child_process");
const pty = require("node-pty");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CLIENT_CONFIG = path.join(os.homedir(), ".lite-work", "client.json");
const LOG_DIR = path.join(os.homedir(), ".lite-work", "logs");
const ELECTRON_LOG_FILE = path.join(LOG_DIR, "electron.log");
const LOG_MAX_BYTES = 5 * 1024 * 1024;
const LOG_BACKUP_COUNT = 3;

function rotateLogFile() {
  try {
    if (!fs.existsSync(ELECTRON_LOG_FILE) || fs.statSync(ELECTRON_LOG_FILE).size < LOG_MAX_BYTES) return;
    for (let index = LOG_BACKUP_COUNT - 1; index >= 1; index -= 1) {
      const source = `${ELECTRON_LOG_FILE}.${index}`;
      const target = `${ELECTRON_LOG_FILE}.${index + 1}`;
      if (fs.existsSync(source)) fs.renameSync(source, target);
    }
    fs.renameSync(ELECTRON_LOG_FILE, `${ELECTRON_LOG_FILE}.1`);
  } catch {
    // 日志写入失败不应影响桌面应用启动。
  }
}

function writeLog(level, ...messages) {
  const text = messages.map((message) => (
    message instanceof Error ? message.stack || message.message : String(message)
  )).join(" ");
  console[level](`[lite-work] ${text}`);
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    rotateLogFile();
    fs.appendFileSync(ELECTRON_LOG_FILE, `${new Date().toISOString()} ${level.toUpperCase()} ${text}\n`, "utf-8");
  } catch {
    // 同上：磁盘或权限异常时仅保留控制台输出。
  }
}
let coreMode = "local"; // "local" | "remote" | "dev"
const localInstances = new Map(); // webContents.id -> { window, child, url, workspace }
const terminals = new Map(); // webContents.id -> pty process

function loadClientConfig() {
  try {
    if (fs.existsSync(CLIENT_CONFIG)) {
      return JSON.parse(fs.readFileSync(CLIENT_CONFIG, "utf-8"));
    }
  } catch (err) {
    writeLog("warn", "客户端配置读取失败:", err.message);
  }
  return { coreUrl: "", token: "" };
}

function createWindow(url) {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: "#0d1117",
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 18 },
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
      // 把应用版本传给 preload（app.getVersion 自动读 package.json，dev/打包都正确）
      additionalArguments: [`--litework-version=${app.getVersion()}`],
    },
  });

  // 立即显示窗口 + 内置加载页，避免等待后端就绪时空白；默认最大化打开
  window.maximize();
  window.once("ready-to-show", () => window.show());
  window.loadURL(url);
  window.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: "deny" };
  });
  // preload 注入失败时输出错误，便于排查
  window.webContents.on("preload-error", (event, preloadPath, error) => {
    writeLog("error", `preload 加载失败: ${preloadPath}`, error.message);
  });
  // 渲染进程崩溃 / 白屏自动恢复
  window.webContents.on("render-process-gone", (event, details) => {
    writeLog("error", "渲染进程异常:", details.reason);
    setTimeout(() => {
      if (!window.isDestroyed()) {
        window.reload();
      }
    }, 1000);
  });
  // 页面加载失败（后端未就绪等）自动重试
  let failCount = 0;
  window.webContents.on("did-fail-load", (event, code, desc) => {
    failCount += 1;
    writeLog("warn", `页面加载失败(${code}): ${desc}`);
    if (failCount <= 3) {
      setTimeout(() => {
        if (!window.isDestroyed()) window.reload();
      }, 2000);
    }
  });
  window.webContents.on("did-finish-load", () => {
    failCount = 0;
  });
  return window;
}

function resolvePython() {
  // 打包模式：使用内置后端二进制（PyInstaller --onedir 结构：litework-bin/lite-work-backend/lite-work-backend）
  if (app.isPackaged) {
    const isWin = process.platform === "win32";
    const exe = isWin ? "lite-work-backend.exe" : "lite-work-backend";
    const dir = path.join(process.resourcesPath, "litework-bin", "lite-work-backend");
    const bundled = path.join(dir, exe);
    if (fs.existsSync(bundled)) return bundled;
    // 兼容旧版单文件
    const legacy = path.join(process.resourcesPath, "litework-bin", exe);
    if (fs.existsSync(legacy)) return legacy;
  }
  // 开发模式：优先项目 venv，其次系统 python3
  const projectRoot = app.getAppPath();
  const venv = process.platform === "win32"
    ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
    : path.join(projectRoot, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return venv;
  return process.platform === "win32" ? "python" : "python3";
}

// cwd 必须是真实目录：打包模式下 app.getAppPath() 指向 app.asar（文件），会导致 spawn ENOTDIR
function coreCwd() {
  if (app.isPackaged) return process.resourcesPath;
  return app.getAppPath();
}

function stopCore(child) {
  if (child) {
    try {
      child.kill("SIGTERM");
    } catch {
      /* ignore */
    }
  }
}

function spawnLocalCore(workspace) {
  return new Promise((resolve, reject) => {
    const python = resolvePython();
    const projectRoot = coreCwd();
    const args = app.isPackaged
      ? ["serve", "--port", "0"]
      : ["-m", "litework", "serve", "--port", "0"];
    args.push("--config-dir", path.join(os.homedir(), ".lite-work"));
    if (workspace) args.push("--workspace", workspace);
    const env = {
      ...process.env,
      // Finder 启动 Electron 时不会继承 shell PATH；覆盖 Homebrew/MacPorts
      // 等常见 ripgrep 安装位置，Python 后端也会继续使用绝对路径探测。
      PATH: [
        process.env.PATH || "",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        path.join(process.env.HOME || "", ".cargo/bin"),
        path.join(process.env.HOME || "", ".local/bin"),
      ].filter(Boolean).join(path.delimiter),
      LITEWORK_SPAWNED: "1",
    };

    writeLog("log", `启动本地 Core: ${python} ${args.join(" ")}`);
    const child = spawn(python, args, {
      cwd: projectRoot,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let resolved = false;
    const timer = setTimeout(() => {
      if (!resolved) {
        child.kill("SIGKILL");
        reject(new Error("后端启动超时（60s 内未就绪）"));
      }
    }, 60000);

    const onData = (buf) => {
      const text = buf.toString();
      process.stdout.write(text);
      for (const line of text.split(/\r?\n/)) {
        if (line) writeLog("log", `Core: ${line}`);
      }
      const m = text.match(/LITEWORK_CORE_READY port=(\d+)/);
      if (m && !resolved) {
        resolved = true;
        clearTimeout(timer);
        const port = parseInt(m[1], 10);
        resolve({ child, url: `http://127.0.0.1:${port}` });
      }
    };

    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => {
      if (!resolved) {
        clearTimeout(timer);
        reject(new Error(`后端进程提前退出（code=${code}）`));
      }
    });

  });
}

function injectRemoteToken(token) {
  if (!token) return;
  session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
    details.requestHeaders["Authorization"] = `Bearer ${token}`;
    callback({ requestHeaders: details.requestHeaders });
  });
}

// ------------------------------------------------------------ 打开项目

// 热切换工作区：调用当前后端的 /api/workspace，进程不重启（快）
async function hotSwitchWorkspace(instance, workspace) {
  if (!instance?.url) return { ok: false, error: "当前窗口没有可用 Core" };
  try {
    const resp = await fetch(`${instance.url}/api/workspace`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: workspace }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, error: body.detail || `切换工作区失败（HTTP ${resp.status}）` };
    }
    const data = await resp.json();
    if (!data.ok) return { ok: false, error: "切换工作区失败" };
    instance.workspace = workspace;
    if (instance.window && !instance.window.isDestroyed()) {
      stopTerminal(instance.window.webContents.id);
    }
    writeLog("log", `热切换工作区 → ${workspace}`);
    return { ok: true };
  } catch (err) {
    writeLog("warn", "热切换工作区失败:", err.message);
    return { ok: false, error: err.message };
  }
}

async function chooseWorkspace(owner) {
  const result = await dialog.showOpenDialog(owner, {
    title: "选择要打开的项目目录",
    buttonLabel: "打开项目",
    properties: ["openDirectory", "createDirectory"],
  });
  return result.canceled || result.filePaths.length === 0 ? null : result.filePaths[0];
}

async function handleOpenProject(event) {
  // 仅本地 Core 形态支持切换工作区
  if (coreMode !== "local") {
    return { ok: false, error: "当前为远程/开发模式，不支持切换工作区" };
  }

  const owner = BrowserWindow.fromWebContents(event.sender);
  const workspace = await chooseWorkspace(owner);
  if (!workspace) return { ok: false, error: "cancelled" };
  const instance = localInstances.get(event.sender.id);

  // 只允许当前 Core 热切换，避免新进程丢失现有模型配置和运行状态。
  const result = await hotSwitchWorkspace(instance, workspace);
  return result.ok ? { ok: true, url: instance.url, workspace } : result;
}

ipcMain.handle("open-project", handleOpenProject);

// 用系统默认应用打开工作区内的文件/目录（文件→默认应用；目录→系统文件管理器）
ipcMain.handle("open-file", async (event, relPath) => {
  try {
    if (typeof relPath !== "string" || !relPath) return { ok: false, error: "缺少路径" };
    const instance = localInstances.get(event.sender.id);
    if (!instance?.workspace) return { ok: false, error: "未打开项目" };
    const abs = path.resolve(instance.workspace, relPath);
    // 路径越界防护：仅允许打开工作区内路径（含工作区目录本身）。
    // Windows 文件系统大小写不敏感：统一小写后比较
    const wsAbs = path.resolve(instance.workspace);
    const lowerAbs = abs.toLowerCase();
    const lowerWs = wsAbs.toLowerCase() + path.sep;
    if (lowerAbs !== wsAbs.toLowerCase() && !lowerAbs.startsWith(lowerWs)) {
      return { ok: false, error: "路径越界：仅支持打开工作区内的路径" };
    }
    if (!fs.existsSync(abs)) {
      return { ok: false, error: `路径不存在: ${relPath}` };
    }
    const errMsg = await shell.openPath(abs);
    return errMsg ? { ok: false, error: errMsg } : { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

// 在系统文件管理器中定位（高亮显示）工作区内的文件
ipcMain.handle("show-in-folder", async (event, relPath) => {
  try {
    if (typeof relPath !== "string" || !relPath) return { ok: false, error: "缺少路径" };
    const instance = localInstances.get(event.sender.id);
    if (!instance?.workspace) return { ok: false, error: "未打开项目" };
    const abs = path.resolve(instance.workspace, relPath);
    const wsAbs = path.resolve(instance.workspace);
    // Windows 大小写不敏感：统一小写比较
    if (!abs.toLowerCase().startsWith(wsAbs.toLowerCase() + path.sep)) {
      return { ok: false, error: "路径越界：仅支持定位工作区内的文件" };
    }
    if (!fs.existsSync(abs) || !fs.statSync(abs).isFile()) {
      return { ok: false, error: `文件不存在: ${relPath}` };
    }
    shell.showItemInFolder(abs);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

async function createLocalWindow(workspace = null) {
  const loadingUrl = `file://${path.join(__dirname, "loading.html")}`;
  const window = createWindow(loadingUrl);
  try {
    const instance = await spawnLocalCore(workspace);
    const record = { window, ...instance, workspace };
    const winId = window.webContents.id;
    localInstances.set(winId, record);
    window.once("closed", () => {
      stopTerminal(winId);
      stopCore(instance.child);
      localInstances.delete(winId);
    });
    if (!window.isDestroyed()) window.loadURL(instance.url);
    return { ok: true, workspace, url: instance.url };
  } catch (err) {
    if (!window.isDestroyed()) window.destroy();
    return { ok: false, error: err.message };
  }
}

ipcMain.handle("open-project-new-window", async (event) => {
  if (coreMode !== "local") return { ok: false, error: "当前模式不支持新建本地项目窗口" };
  const workspace = await chooseWorkspace(BrowserWindow.fromWebContents(event.sender));
  return workspace ? createLocalWindow(workspace) : { ok: false, error: "cancelled" };
});

function stopTerminal(id) {
  const term = terminals.get(id);
  if (term) {
    // kill 是异步的：pty 缓冲输出可能在此之后仍触发 onData/onExit 回调。
    // 先从表中移除并丢弃监听依赖，回调侧依赖 sender 存活检查兜底。
    terminals.delete(id);
    try { term.kill(); } catch { /* ignore */ }
  }
}

// 渲染进程可能不经「打开项目」对话框、直接通过 HTTP /api/workspace 切换工作区
// （如双击历史会话切到其关联项目）。此时 Electron 主进程的 instance.workspace
// 不会自动更新，而终端启动依赖它作为 cwd / 空值判定，会造成
// 「文件树已是新项目、终端却提示请先打开项目（或 cd 进旧项目）」的不一致。
// 渲染进程在 setWorkspace 成功后主动通知主进程同步，并顺带停掉旧目录里的终端。
ipcMain.on("workspace-changed", (event, workspace) => {
  const instance = localInstances.get(event.sender.id);
  if (!instance || typeof workspace !== "string" || !workspace) return;
  const previous = instance.workspace;
  instance.workspace = workspace;
  stopTerminal(event.sender.id);
  writeLog("log", `渲染进程同步工作区 → ${workspace}${previous && previous !== workspace ? `（原 ${previous}）` : ""}`);
});

ipcMain.handle("terminal-start", (event, cols = 100, rows = 24) => {
  const instance = localInstances.get(event.sender.id);
  if (!instance?.workspace) return { ok: false, error: "请先打开项目" };
  stopTerminal(event.sender.id);
  const shellName = process.platform === "win32" ? "powershell.exe" : (process.env.SHELL || "/bin/zsh");
  const term = pty.spawn(shellName, [], {
    name: "xterm-256color", cols, rows, cwd: instance.workspace, env: process.env,
  });
  const winId = event.sender.id;
  const sender = event.sender;
  // 窗口关闭后 webContents 被销毁，但 pty 回调可能仍在触发（kill 异步 + 输出缓冲）。
  // 对已销毁的 webContents 调用 send 会抛 "Object has been destroyed"，
  // 必须在每次发送前检查存活状态（与 render-process-gone 的 reload 同一原则）。
  const safeSend = (channel, payload) => {
    if (!sender.isDestroyed()) sender.send(channel, payload);
  };
  terminals.set(winId, term);
  term.onData((data) => safeSend("terminal-data", data));
  term.onExit(({ exitCode }) => {
    safeSend("terminal-exit", exitCode);
    terminals.delete(winId);
  });
  return { ok: true };
});
ipcMain.on("terminal-input", (event, data) => terminals.get(event.sender.id)?.write(data));
ipcMain.on("terminal-resize", (event, cols, rows) => terminals.get(event.sender.id)?.resize(cols, rows));
ipcMain.on("terminal-stop", (event) => stopTerminal(event.sender.id));

// ------------------------------------------------------------ 应用菜单

// About 菜单不使用 Electron 原生面板，改为通知渲染进程弹出设计版「关于」对话框
function showAbout() {
  for (const instance of localInstances.values()) {
    if (instance.window && !instance.window.isDestroyed()) {
      instance.window.webContents.send("show-about");
    }
  }
}

function setupMenu() {
  const isMac = process.platform === "darwin";
  const aboutItem = { label: `关于 ${app.name}`, click: showAbout };
  const template = [
    ...(isMac ? [{
      label: app.name,
      submenu: [
        aboutItem,
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit" },
      ],
    }] : []),
    { role: "fileMenu" },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" },
    ...(isMac ? [] : [{ role: "help", submenu: [aboutItem] }]),
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------ 启动

app.whenReady().then(async () => {
  setupMenu();

  // 开发模式：直接加载 Vite dev server
  if (process.env.LITEWORK_DEV_URL) {
    coreMode = "dev";
    createWindow(process.env.LITEWORK_DEV_URL);
    app.on("window-all-closed", () => app.quit());
    return;
  }

  const config = loadClientConfig();

  // 形态2：远程 Core
  if (config.coreUrl) {
    coreMode = "remote";
    injectRemoteToken(config.token);
    writeLog("log", `连接远程 Core: ${config.coreUrl}`);
    createWindow(config.coreUrl);
    app.on("window-all-closed", () => app.quit());
    return;
  }

  // 形态1：本地 Core
  coreMode = "local";

  // 立即创建窗口 + 加载页（即使后端未就绪）
  await createLocalWindow();
});

app.on("window-all-closed", () => {
  for (const instance of localInstances.values()) {
    if (instance.child) stopCore(instance.child);
  }
  localInstances.clear();
  if (process.platform !== "darwin") app.quit();
});
