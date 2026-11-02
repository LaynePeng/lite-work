"""ACP 真实子进程端到端联调（手工运行，不进 pytest）。

    .venv/bin/python tests/e2e_acp.py

为什么单独放这里而不进 pytest：它会拉起真实子进程、依赖真实 stdio 管道，
在 CI 的沙箱里有环境噪音风险；而它验证的又恰恰是单测覆盖不到的那部分
（stdout 是否被日志污染、握手是否真的能通、进程能否干净退出）。

不依赖 LLM：只跑 initialize / session/new / 未知方法 / 坏 JSON / EOF 退出。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time


def main() -> int:
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python = os.path.join(project, ".venv", "bin", "python")
    if not os.path.exists(python):
        python = sys.executable
    env = dict(os.environ)
    env["LITEWORK_STRICT_EVENTS"] = "1"
    cfg = tempfile.mkdtemp(prefix="litework_acp_e2e_")

    proc = subprocess.Popen(
        [python, "-m", "litework", "acp", "--config-dir", cfg, "--log-level", "error"],
        cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_msg(timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                print(f"❌ stdout 出现非 JSON 内容: {line[:200]!r}")
                return None
        return None

    results = []

    def check(name, ok, detail=None):
        results.append((name, ok))
        print(("✅" if ok else "❌"), name, ("→ " + str(detail)[:200]) if detail and not ok else "")

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": 1, "clientCapabilities": {}}})
    msg = read_msg()
    check("initialize 握手（stdout 为合法 JSON-RPC）",
          bool(msg and msg.get("id") == 1
               and msg.get("result", {}).get("protocolVersion") == 1), msg)

    send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
          "params": {"cwd": "/tmp", "mcpServers": []}})
    msg = read_msg()
    sid = (msg or {}).get("result", {}).get("sessionId")
    check("session/new 返回 sessionId", bool(sid), sid)

    send({"jsonrpc": "2.0", "id": 3, "method": "no/such/method", "params": {}})
    msg = read_msg()
    check("未知方法回 -32601 且不崩溃",
          bool(msg and msg.get("error", {}).get("code") == -32601), msg)

    proc.stdin.write("{ this is not json }\n")
    proc.stdin.flush()
    msg = read_msg(timeout=10)
    check("坏 JSON 回 -32700 且不崩溃",
          bool(msg and msg.get("error", {}).get("code") == -32700), msg)

    proc.stdin.close()
    try:
        code = proc.wait(timeout=30)
        check("stdin EOF 后干净退出", code == 0, code)
    except subprocess.TimeoutExpired:
        proc.kill()
        check("stdin EOF 后干净退出", False, "超时，需强杀")

    stderr_out = proc.stderr.read()
    if stderr_out.strip():
        print("\n--- stderr（日志应在这里）---")
        print(stderr_out[-600:])

    failed = [n for n, ok in results if not ok]
    print("\n结果:", "全部通过" if not failed else f"{len(failed)} 项失败")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
