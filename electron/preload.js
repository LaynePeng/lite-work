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
   * 通用目录选择（不切换工作区）。新建任务向导「任意目录」落盘位置用。
   * 返回 { ok: true, path } 或 { ok: false, error: "cancelled" }。
   */
  chooseDirectory: () => ipcRenderer.invoke("choose-directory"),
  /**
   * 重启当前窗口的本地 Core（插件变更后换取全新进程状态）。
   * 成功后页面整页刷新；有任务运行中时返回 { ok: false, error }。
   */
  restartCore: () => ipcRenderer.invoke("restart-core"),
  /** 局域网共享状态：{ enabled, url, token, ips }（url/token 仅开启时非空） */
  getLanShare: () => ipcRenderer.invoke("get-lan-share"),
  /** 开关局域网共享（写 client.json；需重启 Core 生效） */
  setLanShare: (enabled) => ipcRenderer.invoke("set-lan-share", enabled),
  /** Core 连接：当前模式（local/remote + 显示名）与已保存的远程列表 */
  getCoreMode: () => ipcRenderer.invoke("get-core-mode"),
  /** 添加/覆盖一个远程 Core 配置（同 URL 覆盖）。返回 { ok, cores } */
  coreAdd: (entry) => ipcRenderer.invoke("core-add", entry),
  /** 移除远程 Core 配置；删的是当前连接时下次启动回本地。返回 { ok, cores } */
  coreRemove: (url) => ipcRenderer.invoke("core-remove", url),
  /** 切换连接（url 空 = 回本地），成功后应用自动重启。返回 { ok, restarting } */
  coreSwitch: (url) => ipcRenderer.invoke("core-switch", url),
  /**
   * 用系统默认应用打开工作区内的文件（docx/xlsx/pdf/图片等非代码文件）。
   * 返回 { ok: true } 或 { ok: false, error }。
   */
  openFile: (path) => ipcRenderer.invoke("open-file", path),
  /**
   * 在系统文件管理器中定位（高亮显示）工作区内的文件。
   * 返回 { ok: true } 或 { ok: false, error }。
   */
  showInFolder: (path) => ipcRenderer.invoke("show-in-folder", path),
  /**
   * 富块导出：「另存为」对话框 + 写入（base64，二进制安全）。路径只能来自
   * 用户的原生选择——渲染进程不提供路径，因此不构成任意文件写入口。
   * 用户取消 → { ok: false, error: "cancelled" }；成功 → { ok: true, path }。
   */
  saveFile: (options) => ipcRenderer.invoke("save-file", options),
  /**
   * 富块导出：把 base64 内容写进工作区内文件（用于「导出到项目」，
   * 产物可直接被 docx_create 等工具引用）。返回 { ok: true, path } 或 { ok: false, error }。
   */
  writeWorkspaceFile: (relPath, base64) => ipcRenderer.invoke("write-workspace-file", relPath, base64),
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
