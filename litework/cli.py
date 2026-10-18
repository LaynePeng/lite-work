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
    pair = sub.add_parser("pair", help="打印手机配对二维码（对运行中的 Core 探测地址与令牌）")
    pair.add_argument("--url", default="", help="Core 地址（默认自动探测本机运行实例）")
    pair.add_argument("--token", default="", help="访问令牌（默认从 Core 的配置目录读取）")
    pair.add_argument("--config-dir", default=None, help="配置目录（默认 ~/.lite-work，从中读 server-token）")

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


def _load_or_create_token(config_dir: str | None) -> str:
    """读取/生成访问令牌并落盘 `server-token`（跨重启稳定）。

    桌面本地 Core 每次启动都是新进程：若每次都重新生成随机令牌，已配对的
    手机/浏览器下次连接必然 401（扫码配对形同一次性）。这与容器入口的策略
    一致（`deploy/docker-entrypoint.sh` 首启生成一次、落卷持久），CLI 侧此前
    漏了这一步。

    优先级：调用方显式传入的 `--token` 已在外层处理；本函数只在未显式指定时
    使用。落盘失败（目录不可写）退化为「本次进程内有效」，不影响启动。
    """
    import secrets

    cfg_dir = os.path.abspath(os.path.expanduser(config_dir or "~/.lite-work"))
    path = os.path.join(cfg_dir, "server-token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        print(f"警告: 令牌无法落盘（{path}）: {exc}——重启后将更换令牌", file=sys.stderr)
    return token


def _first_lan_ipv4() -> str:
    """本机第一个非回环 IPv4（serve 绑 0.0.0.0 时给手机/桌面展示真实地址）。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # 不发包，只为让系统按路由选源地址（UDP connect 是本地操作）
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _print_pair_qr(url: str, token: str) -> None:
    """终端打印手机配对二维码（ASCII 方块）。

    手机扫码 → 打开 `{url}/#litework-token={token}` 自动配对。qrcode
    缺失时退化为打印配对 URL（复制到手机浏览器同样可用）。
    """
    pair_url = f"{url.rstrip('/')}/#litework-token={token}"
    print()
    print("手机配对：相机扫码，或把下面的链接发到手机浏览器打开")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(pair_url)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(安装 qrcode 可显示二维码: pip install qrcode)")
    print(pair_url)
    print()


def _run_pair(args: argparse.Namespace) -> int:
    """`lite-work pair`：探测运行中的 Core，打印手机配对二维码。"""
    import json
    import os
    import urllib.request

    # 令牌优先级：--token > 配置目录 server-token > 空（--no-token 部署）
    token = args.token
    if not token:
        cfg_dir = os.path.abspath(os.path.expanduser(args.config_dir or "~/.lite-work"))
        token_file = os.path.join(cfg_dir, "server-token")
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            token = ""

    if args.url:
        urls = [args.url.rstrip("/")]
    else:
        urls = ["http://127.0.0.1:8787", "http://localhost:8787"]

    for base in urls:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as resp:
                if resp.status == 200 and json.loads(resp.read()).get("status") == "ok":
                    _print_pair_qr(base, token)
                    return 0
        except OSError:
            continue
    print("未探测到运行中的 Core（默认 127.0.0.1:8787）。", file=sys.stderr)
    print("用法: lite-work pair --url http://<地址>:<端口> --token <令牌>", file=sys.stderr)
    return 1


def main(argv: list | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.version:
        print(f"lite-work {VERSION}")
        return

    if args.command == "warmup":
        sys.exit(_run_warmup())

    if args.command == "pair":
        sys.exit(_run_pair(args))

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
        token = _load_or_create_token(args.config_dir)

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
            # 对外地址：手机/桌面直连用（127.0.0.1 只在本机有意义）。
            # 多网卡时取第一个非回环 IPv4；拿不到就退回 host 原值。
            lan_host = args.host
            if args.host in ("0.0.0.0", "::"):
                lan_host = _first_lan_ipv4() or "127.0.0.1"
            print(f"lite-work Core 已启动 → http://{lan_host}:{port}", flush=True)
            if token:
                print(f"鉴权已开启，访问令牌: {token}", flush=True)
                # Docker/自托管形态：日志里直接打印手机配对二维码，
                # docker compose logs core 即可扫码，无需进容器敲命令
                _print_pair_qr(f"http://{lan_host}:{port}", token)
            else:
                print("⚠️  警告: 鉴权已关闭（--no-token），任何能访问该端口的进程均可控制本服务", flush=True)

    # access_log=True 时每条请求都记一行：前端每 2s 轮询 /api/tasks/background，
    # 长时间挂机足以把日志刷成噪声并白白吃掉磁盘 I/O（--log-level debug 时又希望
    # 保留它排查）。故默认按 log_level 推导：仅 debug 开 access log。
    server = _Server(uvicorn.Config(
        fast_app, host=args.host, port=args.port, log_level=args.log_level,
        access_log=args.log_level == "debug",
    ))
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
