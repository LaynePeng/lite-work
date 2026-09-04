import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

export default function TerminalPanel({ workspace }: { workspace: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  // 有效工作区：非空且非哨兵值（Sidebar 无项目时传"未打开项目"）
  const validWorkspace = workspace && workspace !== "未打开项目" ? workspace : null;

  useEffect(() => {
    const bridge = window.liteWork;
    const host = hostRef.current;
    if (!bridge || !host || !validWorkspace) return;

    const terminal = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontSize: 12,
      fontFamily: "SFMono-Regular, Menlo, Consolas, monospace",
      theme: { background: "#0d1117", foreground: "#d1d5db", cursor: "#7aa2f7" },
      scrollback: 5000,
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host);
    fit.fit();

    const offData = bridge.onTerminalData((data) => terminal.write(data));
    const offExit = bridge.onTerminalExit((code) => terminal.writeln(`\r\n[终端已退出: ${code}]`));
    const input = terminal.onData((data) => bridge.terminalInput(data));
    const resize = new ResizeObserver(() => {
      fit.fit();
      bridge.terminalResize(terminal.cols, terminal.rows);
    });
    resize.observe(host);
    void bridge.terminalStart(terminal.cols, terminal.rows).then((result) => {
      if (!result.ok) setError(result.error ?? "终端启动失败");
    });

    return () => {
      resize.disconnect();
      input.dispose();
      offData();
      offExit();
      bridge.terminalStop();
      terminal.dispose();
    };
  }, [validWorkspace]);

  if (!window.liteWork) return <div className="terminal-empty">终端仅在桌面应用中可用</div>;
  if (!validWorkspace) return <div className="terminal-empty">打开项目后启动终端</div>;
  return <div className="terminal-wrap">{error && <div className="terminal-error">{error}</div>}<div className="terminal-host" ref={hostRef} /></div>;
}
