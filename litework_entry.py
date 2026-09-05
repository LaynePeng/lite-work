"""PyInstaller 打包入口：以绝对导入启动 lite-work CLI。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 默认编码是 GBK（中文系统），子进程 stdout/stderr 读 UTF-8 会
# UnicodeDecodeError。进入应用前强制 UTF-8 模式（等价 PYTHONUTF8=1）。
if sys.platform == "win32" and sys.stdout.encoding and sys.stdout.encoding.lower().startswith("gb"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from litework.cli import main  # noqa: E402

if __name__ == "__main__":
    main()