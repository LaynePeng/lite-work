"""ACP 全链路认证：假 LLM 服务器 + 真实 AgentLoop + 真实工具 + 真实审批门。

不开编辑器、不需要真 API Key，但走的是**生产同一条代码路径**：

    真实 ACP 子进程 ←stdio→ AcpAgent ←→ AgentLoop ←→ 真实工具注册表
                                    ↑
                          假 LLM（标准 OpenAI 兼容 SSE）

为什么要造假 LLM：真 LLM 不可复现（同样的 prompt 每次答案不同、要花钱、
没网就跑不了）。而审批与工具渲染的正确性只取决于「模型是否请求了某个工具」，
与模型是真是假无关——只要它返回标准格式的 tool_calls，后面全是生产代码。

用法：.venv/bin/python scripts/verify-acp-flow.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 假 LLM

#: 第一轮让模型「读文件」，第二轮给最终答复。
#: 工具结果会以 tool 消息回灌，这里用简单计数决定该回什么。
_call_count = {"n": 0}


class FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        msgs = payload.get("messages") or []
        # 按「已经产生了几个 tool 结果消息」决定该回什么，而不是靠全局计数——
        # 后者在重试/多会话时会错位，导致永远跳不到第二阶段。
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        round_no = len(tool_msgs) + 1

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        if round_no == 1:
            # 第一轮：请求调用 read_file
            chunks = [
                {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "call_read_1", "type": "function",
                    "function": {"name": "read_file", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": json.dumps({"filePath": "README.md"})}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        elif round_no == 2:
            # 已有一个工具结果 → 请求 delete_file（删除**一律**走审批）。
            # 选它而非 write_file：写工作区内的文件默认免审批，用后者验不到审批桥。
            chunks = [
                {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
                {"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "call_del_1", "type": "function",
                    "function": {"name": "delete_file", "arguments": json.dumps(
                        {"filePath": "obsolete.txt"})}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        else:
            chunks = [
                {"choices": [{"delta": {"role": "assistant",
                                       "content": "已完成读取与写入。"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]

        for ch in chunks:
            self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args) -> None:
        pass


def start_fake_llm() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ---------------------------------------------------------------- 主流程

def main() -> int:
    srv, llm_port = start_fake_llm()
    cfg = tempfile.mkdtemp(prefix="litework_acp_verify_")
    # 预置配置：把默认 provider 指到假服务器
    os.makedirs(cfg, exist_ok=True)
    config = {
        "llm": {
            "active": "deepseek",
            "providers": {
                "deepseek": {
                    "api_key": "sk-fake-for-verification",
                    "base_url": f"http://127.0.0.1:{llm_port}",
                    "model": "deepseek-chat",
                }
            },
        },
        # 关键：不要 auto_approve，否则审批桥不会被触发，等于没验
        "auto_approve": False,
    }
    with open(os.path.join(cfg, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)

    py = os.path.join(PROJECT, ".venv", "bin", "python")
    env = dict(os.environ)
    env["LITEWORK_STRICT_EVENTS"] = "1"
    # 打开协程未 await 追踪：能把「哪个文件哪一行」打出来，
    # 否则只有一句 RuntimeWarning，无法定位
    env["PYTHONWARNINGS"] = "always"
    if os.environ.get("ACP_TRACE_LEAK") == "1":
        env["PYTHONTRACEMALLOC"] = "25"
    work = tempfile.mkdtemp(prefix="litework_acp_ws_")
    with open(os.path.join(work, "README.md"), "w") as f:
        f.write("# 测试项目\n\n这是被读取的文件。\n")
    # delete_file 的目标：存在才删得掉，用「审批后文件消失」反证工具真的执行了
    with open(os.path.join(work, "obsolete.txt"), "w") as f:
        f.write("待删除的文件\n")

    proc = subprocess.Popen(
        [py, "-m", "litework", "acp", "--config-dir", cfg, "--log-level", "warning"],
        cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, text=True, bufsize=1)

    def send(o):
        proc.stdin.write(json.dumps(o) + "\n")
        proc.stdin.flush()

    seen = {"tool_calls": [], "tool_updates": [], "perm_requests": [], "text": []}

    def pump(until_id, timeout=120):
        """读到指定响应 id 为止，期间收集所有通知与反向请求。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return False
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                print(f"❌ stdout 非 JSON: {line[:200]!r}")
                return False
            method = m.get("method")
            if method == "session/update":
                up = m["params"]["update"]
                kind = up.get("sessionUpdate")
                if kind == "tool_call":
                    seen["tool_calls"].append(up)
                elif kind == "tool_call_update":
                    seen["tool_updates"].append(up)
                elif kind == "agent_message_chunk":
                    t = (up.get("content") or {}).get("text")
                    if t:
                        seen["text"].append(t)
            elif method == "session/request_permission":
                seen["perm_requests"].append(m)
                # 模拟用户在编辑器里的选择；ACCEPT 环境变量控制允许/拒绝
                opt = "allow-once" if os.environ.get("ACP_E2E_DENY") != "1" else "reject-once"
                send({"jsonrpc": "2.0", "id": m["id"], "result": {
                    "outcome": {"outcome": "selected", "optionId": opt}}})
            if m.get("id") == until_id and "method" not in m:
                return m
        return False

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(("✅" if ok else "❌"), name, (f"→ {detail}" if detail else ""))

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": 1, "clientCapabilities": {}}})
    init = pump(1)
    check("initialize 握手", bool(init and init.get("result")), str(init)[:80])

    send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
          "params": {"cwd": work, "mcpServers": []}})
    new = pump(2)
    sid = (new or {}).get("result", {}).get("sessionId") if isinstance(new, dict) else None
    check("session/new", bool(sid), str(sid))

    print("\n→ 发送 prompt（真实 AgentLoop + 假 LLM，会让模型读写文件）…")
    send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": "读 README 然后写个文件"}]}})
    final = pump(3, timeout=180)

    check("prompt 正常收尾", isinstance(final, dict) and "result" in final,
          json.dumps(final, ensure_ascii=False)[:160] if final else "无响应")

    # —— 第 4 项：工具调用可视化
    tc = seen["tool_calls"]
    check("收到 tool_call 通知", len(tc) >= 1, f"{len(tc)} 条")
    if tc:
        names = [t.get("name") for t in tc]
        check("tool_call 带工具名", all(names), str(names))
        check("tool_call 带 kind（编辑器选图标用）",
              all(t.get("kind") for t in tc), str([t.get("kind") for t in tc]))
        check("tool_call 带 title", all(t.get("title") for t in tc),
              str([t.get("title") for t in tc])[:120])
    check("收到 tool_call_update（结果回填）", len(seen["tool_updates"]) >= 1,
          f"{len(seen['tool_updates'])} 条")
    if seen["tool_updates"]:
        st = [u.get("status") for u in seen["tool_updates"]]
        check("tool_call_update 状态合法",
              all(s in ("completed", "failed") for s in st), str(st))
        if os.environ.get("ACP_E2E_DENY") == "1":
            # 拒绝场景：被拦住的工具必须显示 failed，不能显示 completed
            # （否则用户会以为「删除已完成」）
            check("被拒绝的工具显示为 failed（不误导用户）",
                  "failed" in st, str(st))

    # —— 第 5 项：审批桥
    perms = seen["perm_requests"]
    check("收到 session/request_permission（审批桥生效）", len(perms) >= 1,
          f"{len(perms)} 次")
    if perms:
        p = perms[0]["params"]
        check("权限请求带 options", bool(p.get("options")),
              str([o.get("optionId") for o in p.get("options", [])]))

    # —— 删除是否真的发生（证明审批放行后工具真的执行了）
    deleted = os.path.join(work, "obsolete.txt")
    deny = os.environ.get("ACP_E2E_DENY") == "1"
    if deny:
        # 反例：用户拒绝 → 文件必须仍在（工具真的没执行）
        check("用户拒绝时文件未被删除（审批真的拦住了）", os.path.exists(deleted),
              "仍存在" if os.path.exists(deleted) else "被删了（审批未生效）")
    else:
        check("审批放行后文件真的被删除", not os.path.exists(deleted),
              "已删除" if not os.path.exists(deleted) else "文件仍在（工具未执行）")

    # —— 文本流
    check("收到 agent 文本输出", len(seen["text"]) >= 1, "".join(seen["text"])[:100])

    proc.stdin.close()
    try:
        code = proc.wait(timeout=20)
        check("进程干净退出", code == 0, str(code))
    except subprocess.TimeoutExpired:
        proc.kill()
        check("进程干净退出", False, "超时")

    err = proc.stderr.read()
    if err.strip():
        print("\n--- stderr 摘要 ---")
        print(err[-1200:])

    failed = [n for n, ok in results if not ok]
    print(f"\n===== 结果：{len(results) - len(failed)}/{len(results)} 通过 =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
