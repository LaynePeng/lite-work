# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""lite-work CLI：无头 Core 服务启动入口。

用法:
  lite-work serve --host 127.0.0.1 --port 0 --token xxx --workspace /path
  lite-work serve --port 8787          # 默认端口
  lite-work --version

启动成功后输出一行机器可读的就绪标记供 Electron 解析:
  LITEWORK_CORE_READY port=8787 workspace=/path
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import uvicorn

from . import __version__
from .app import AgentApp
from .server.app import create_app

VERSION = __version__
LOG_FILE_NAME = "lite-work.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def _parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lite-work",
        description="lite-work Core：手写的 Code 开发 Agent 服务",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 Core 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--token", default=None, help="访问令牌；缺省时自动生成")
    serve.add_argument("--no-token", action="store_true",
                       help="显式关闭鉴权（仅建议本机开发调试使用）")
    serve.add_argument("--workspace", default=None, help="工作区目录（默认不打开项目）")
    serve.add_argument("--config-dir", default=None, help="配置/会话目录（默认 ~/.lite-work）")
    serve.add_argument("--api-key", default=None, help="LLM API Key（默认读 DEEPSEEK_API_KEY）")
    serve.add_argument("--base-url", default=None, help="OpenAI 兼容 base_url（默认 DeepSeek）")
    serve.add_argument("--model", default=None, help="模型名（默认 deepseek-flash）")
    serve.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])

    warmup = sub.add_parser("warmup", help="预热本地缓存（matplotlib 字体缓存，供安装阶段调用）")

    parser.add_argument("--version", action="store_true", help="显示版本")

    return parser.parse_args(argv)


def _configure_logging(log_level: str, config_dir: str | None) -> str:
    """同时输出到终端与用户配置目录下的滚动日志文件。"""
    log_dir = os.path.join(
        os.path.abspath(os.path.expanduser(config_dir or "~/.lite-work")), "logs"
    )
    log_path = os.path.join(log_dir, LOG_FILE_NAME)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                encoding="utf-8",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
            )
        )
    except OSError as exc:
        # 文件系统不可用时仍应保证 Core 可以启动并输出终端日志。
        print(f"lite-work 日志文件不可用（{log_path}）: {exc}", file=sys.stderr)

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=getattr(logging, log_level.upper()), handlers=handlers, force=True
    )
    return log_path


def _run_warmup() -> int:
    """预热本地缓存（当前仅 matplotlib 字体缓存），供安装阶段调用。

    与 serve 共用同一持久缓存目录：打包版由 litework_entry.py 在 frozen
    模式下把 MPLCONFIGDIR 指向 ~/.lite-work/mpl；开发模式走 matplotlib
    默认目录（~/.matplotlib，同样持久）。fail-open：任何异常都不阻断
    安装流程——首次启动的后台预热线程会兜底构建。
    """
    import time as _time

    started = _time.time()
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.font_manager as _fm

        # 公开 API：首次访问会构建 FontManager 单例并落盘字体缓存
        # （不要用 findfont(FontProperties(family=...))：family 字符串会走
        #   mathtext 解析器，在本机 matplotlib 版本上抛 ParseException）
        _fm.get_font_names()
    except Exception as exc:  # noqa: BLE001
        print(f"lite-work warmup 失败（不阻断安装）: {exc}", file=sys.stderr)
        return 0
    print(f"lite-work warmup 完成，耗时 {_time.time() - started:.1f}s", flush=True)
    return 0


def main(argv: list = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.version:
        print(f"lite-work {VERSION}")
        return

    if args.command == "warmup":
        sys.exit(_run_warmup())

    if args.command != "serve":
        print("用法: lite-work serve [--port N] [--token xxx | --no-token] [--workspace /path]")
        sys.exit(1)

    log_path = _configure_logging(args.log_level, args.config_dir)
    logging.getLogger("litework.cli").info("日志文件: %s", log_path)

    # 经就绪标记（token=...）下发给 Electron 桌面外壳自动注入请求头。
    # --no-token 显式关闭（本机开发调试），此时输出醒目告警。
    if args.no_token and args.token:
        print("错误: --token 与 --no-token 不能同时使用", file=sys.stderr)
        sys.exit(1)
    token = args.token
    if token is None and not args.no_token:
        import secrets
        token = secrets.token_urlsafe(24)

    app = AgentApp(
        workspace=args.workspace,
        config_dir=args.config_dir,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    fast_app = create_app(app, token=token)

    class _Server(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            port = args.port
            try:
                if self.servers and self.servers[0].sockets:
                    port = self.servers[0].sockets[0].getsockname()[1]
            except Exception:
                pass
            # 机器可读就绪标记：Electron 主进程据此拿到实际端口与鉴权令牌
            token_part = f" token={token}" if token else ""
            print(f"LITEWORK_CORE_READY port={port} workspace={app.workspace}{token_part}", flush=True)
            print(f"lite-work Core 已启动 → http://{args.host}:{port}", flush=True)
            if token:
                print(f"鉴权已开启，访问令牌: {token}", flush=True)
            else:
                print("⚠️  警告: 鉴权已关闭（--no-token），任何能访问该端口的进程均可控制本服务", flush=True)

    server = _Server(uvicorn.Config(fast_app, host=args.host, port=args.port, log_level=args.log_level))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        import asyncio

        try:
            asyncio.run(app.close())
        except RuntimeError:
            pass


if __name__ == "__main__":
    main()
