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

# 覆盖 PyInstaller 的 matplotlib 运行时钩子（pyi_rth_mplconfig）：
# 该钩子在入口代码之前无条件把 MPLCONFIGDIR 指到一次性临时目录并随进程
# 删除，导致打包版每次启动都全量重建 matplotlib 字体缓存（实测 10-17s，
# 且后台重建线程的 GIL 争用会同步拖慢后端就绪）。这里改指持久化的用户
# 目录，字体缓存只建一次；目录不可写时静默放弃（退回钩子的临时目录）。
if getattr(sys, "frozen", False):
    try:
        _mpl_dir = os.path.join(os.path.expanduser("~"), ".lite-work", "mpl")
        os.makedirs(_mpl_dir, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = _mpl_dir
    except Exception:
        pass

from litework.cli import main  # noqa: E402

if __name__ == "__main__":
    main()