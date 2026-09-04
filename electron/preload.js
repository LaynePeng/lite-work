// 最小化 preload：仅暴露「打开项目」等安全的原生能力
const { contextBridge, ipcRenderer } = require("electron");

// main 进程通过 additionalArguments 注入 app.getVersion()；非 Electron 环境兜底 "dev"
function readVersion() {
  const arg = process.argv.find((a) => a.startsWith("--litework-version="));
  return arg ? arg.slice("--litework-version=".length) : "dev";
}

contextBridge.exposeInMainWorld("liteWork", {
  platform: process.platform,
  version: readVersion(),
  /**
   * 打开系统目录选择框并切换工作区。
   * 返回 { ok: true, url } 或 { ok: false, error }；用户在对话框取消时返回 { ok: false, error: "cancelled" }。
   */
  openProject: () => ipcRenderer.invoke("open-project"),
  openProjectNewWindow: () => ipcRenderer.invoke("open-project-new-window"),
  /**
   * 用系统默认应用打开工作区内的文件（docx/xlsx/pdf/图片等非代码文件）。
   * 返回 { ok: true } 或 { ok: false, error }。
   */
  openFile: (path) => ipcRenderer.invoke("open-file", path),
  workspaceChanged: (workspace) => ipcRenderer.send("workspace-changed", workspace),
  terminalStart: (cols, rows) => ipcRenderer.invoke("terminal-start", cols, rows),
  terminalInput: (data) => ipcRenderer.send("terminal-input", data),
  terminalResize: (cols, rows) => ipcRenderer.send("terminal-resize", cols, rows),
  terminalStop: () => ipcRenderer.send("terminal-stop"),
  onTerminalData: (listener) => {
    const handler = (_event, data) => listener(data);
    ipcRenderer.on("terminal-data", handler);
    return () => ipcRenderer.removeListener("terminal-data", handler);
  },
  onTerminalExit: (listener) => {
    const handler = (_event, code) => listener(code);
    ipcRenderer.on("terminal-exit", handler);
    return () => ipcRenderer.removeListener("terminal-exit", handler);
  },
  /** 订阅应用菜单「关于」点击，打开设计版关于弹窗；返回取消订阅函数 */
  onShowAbout: (listener) => {
    const handler = () => listener();
    ipcRenderer.on("show-about", handler);
    return () => ipcRenderer.removeListener("show-about", handler);
  },
});
