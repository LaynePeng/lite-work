# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""代码审查工具（对应课程第14课（实战 Core）总结提出的增强插件：代码审查）。

流程：收集未提交改动（git diff）→ 静态体检（AST 语法错误、常见反模式、
复杂度信号、安全隐患）→ 输出结构化审查报告，供 LLM 汇总成完整评审。
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
from typing import Any, Dict, List

from ..core.types import ToolDefinition
from .ast_tools import ASTAnalyzer

_ANTI_PATTERNS = [
    (r"\bexcept\s*:\s*(pass|continue)?\s*$", "裸 except 吞掉异常"),
    (r"\bexcept\s+Exception\s*:\s*(pass|continue)?\s*$", "大范围 except 无处理"),
    (r"print\(.+\)\s*$", "遗留 print 调试语句"),
    (r"console\.log\(.+\)\s*$", "遗留 console.log 调试语句"),
    (r"TODO|FIXME|HACK", "遗留 TODO/FIXME/HACK 标记"),
    (r"\bpassword\s*=\s*['\"][^'\"]+['\"]", "疑似硬编码密码"),
    (r"\bapi[_ ]?key\s*=\s*['\"][^'\"]+['\"]", "疑似硬编码 API Key"),
    (r"\beval\s*\(", "使用 eval（代码注入风险）"),
    # 光靠 \b 挡不住正则字面量的方法调用：/re/.exec( 的 "." 也是非词字符，
    # 会误命中「使用 exec」（同理 create_subprocess_exec 靠 \b 已能挡住）
    (r"(?<![\w.])exec\s*\(", "使用 exec"),
    (r"shell=True", "subprocess shell=True（注入风险）"),
    (r"git\s+push\s+.*--force", "强制推送"),
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"\bsudo\b", "sudo 提权"),
    (r"while\s+True\s*:", "while True 无限循环（注意退出条件）"),
]

# 语法检查相关阈值。tree-sitter 是「容错解析器」：遇到它不认识的语法会恢复出一个
# ERROR 节点，该节点可能横跨整个文件（起点还停在第 1 行），内部再嵌套若干 ERROR。
# 若把每个 ERROR 节点都逐条上报，就会出现「报在第 1 行」+「同一行重复 N 条」的
# 假象（实测 SettingsModal.tsx 因内联 import("...") 类型被报 84 条）。
_TS_LIKE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_MAX_PARSE_FINDINGS = 5      # 每文件最多列出的解析问题条数，其余折叠为汇总
_RECOVERY_MIN_LINES = 20     # 跨度小于此值不视为「恢复节点」
_RECOVERY_SPAN_RATIO = 0.5   # 且需覆盖文件一半以上行数


class ReviewTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self._analyzer = ASTAnalyzer()

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="review_code",
                description="对当前未提交的代码改动执行静态审查（语法错误/反模式/安全隐患），返回结构化报告",
                parameters={
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "description": "审查范围: 'unstaged'(默认)/'staged'/全部文件路径"},
                    },
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "review_code":
            raise ValueError(f"Unknown Review Tool: {name}")

        scope = args.get("scope") or "unstaged"
        changed_files = await self._get_changed_files(scope)
        if not changed_files:
            return "[Review]: 未发现可审查的代码改动（工作区干净）。"

        lines: List[str] = [f"# 代码审查报告（{scope}，{len(changed_files)} 个文件）", ""]

        for rel_path in changed_files:
            full = os.path.join(self.workspace, rel_path)
            if not os.path.isfile(full):
                continue
            findings = await asyncio.to_thread(self._review_file, full)
            lines.append(f"## {rel_path}")
            if not findings:
                lines.append("- 未发现明显问题。")
            else:
                for finding in findings:
                    lines.append(f"- {finding}")
            lines.append("")

        return "\n".join(lines)

    async def _get_changed_files(self, scope: str) -> List[str]:
        import shutil

        git = shutil.which("git")
        if not git:
            return []
        cmd = [git, "diff", "--name-only"]
        if scope == "staged":
            cmd.append("--cached")
        if scope == "all":
            cmd = [git, "diff", "--name-only", "HEAD"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return [l for l in stdout.decode("utf-8", errors="replace").splitlines() if l.strip()]

    def _review_file(self, full_path: str) -> List[str]:
        findings: List[str] = []
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                code = f.read()
        except OSError:
            return ["无法读取文件"]

        ext = os.path.splitext(full_path)[1].lower()

        # 1. 语法检查（Python 走标准库 ast；TS/JS/Java/Go 走 tree-sitter 容错解析）
        findings.extend(self._check_syntax(code, ext))

        # 2. 反模式扫描
        for pattern, label in _ANTI_PATTERNS:
            for m in re.finditer(pattern, code, re.IGNORECASE):
                line = code[: m.start()].count("\n") + 1
                findings.append(f"{label} (行 {line})")
                break  # 每类只报一次，避免刷屏

        # 3. 大文件 / 长函数复杂度信号
        total_lines = len(code.split("\n"))
        if total_lines > 600:
            findings.append(f"文件过大（{total_lines} 行），建议拆分模块")

        return findings

    def _check_syntax(self, code: str, ext: str) -> List[str]:
        """语法检查：Python 走标准库 ast，TS/JS/Java/Go 走 tree-sitter 容错解析。

        设计要点都来自真实误报：
        · tree-sitter 遇到它不认识的语法会恢复出一个横跨整个文件的 ERROR 节点
          （起点常停在第 1 行），内部再嵌套若干 ERROR——逐节点上报就变成「报第 1
          行」+「同一行重复多条」，故跳过恢复节点并按行去重；
        · 每文件最多列 _MAX_PARSE_FINDINGS 条，其余折叠成一行汇总，避免刷屏；
        · TS/TSX 用「解析告警」措辞并附「以 tsc 为准」提示：tree-sitter 的
          typescript 语法（PyPI 最新 0.23.2）对内联 import("...") 类型等较新语法
          存在误判，tsc 才是权威（实测合法文件被报过 84 处「语法错误」）。
        """
        lines = code.split("\n")

        if ext == ".py":
            try:
                ast.parse(code)
            except SyntaxError as exc:
                snippet = (exc.text or "").strip()[:120]
                tail = f": {snippet}" if snippet else ""
                return [f"语法错误(行 {exc.lineno}){tail}"]
            return []

        tree = self._analyzer.parse(code, ext)
        if tree is None:
            return []

        total = max(1, len(lines))
        label = "解析告警" if ext in _TS_LIKE_EXTS else "语法错误"
        seen: set = set()
        found: List[tuple] = []
        recovery = False

        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.is_error or node.type == "ERROR":
                start = node.start_point[0] + 1
                span = node.end_point[0] - node.start_point[0] + 1
                if span >= _RECOVERY_MIN_LINES and span / total >= _RECOVERY_SPAN_RATIO:
                    recovery = True          # 恢复节点：定位不到具体行，不上报
                elif start not in seen:
                    seen.add(start)
                    snippet = lines[start - 1][:120] if start <= len(lines) else ""
                    found.append((start, f"{label}(行 {start}): {snippet}"))
            stack.extend(node.children)

        found.sort(key=lambda item: item[0])
        findings = [text for _, text in found[:_MAX_PARSE_FINDINGS]]
        hidden = len(found) - _MAX_PARSE_FINDINGS
        if hidden > 0:
            findings.append(f"…另有 {hidden} 处{label}未列出")
        if recovery and not findings:
            findings.append(f"{label}：存在无法定位的解析失败（可能是新语法或严重语法错误）")
        if findings and ext in _TS_LIKE_EXTS:
            findings.append(
                "提示：TS/TSX 的解析告警可能来自 tree-sitter 对较新语法的误判，请以 tsc / eslint 结果为准"
            )
        return findings