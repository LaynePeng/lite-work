"""代码审查工具（对应课程第14课（实战 Core）总结提出的增强插件：代码审查）。

流程：收集未提交改动（git diff）→ 静态体检（AST 语法错误、常见反模式、
复杂度信号、安全隐患）→ 输出结构化审查报告，供 LLM 汇总成完整评审。
"""
from __future__ import annotations

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
    (r"\bexec\s*\(", "使用 exec"),
    (r"shell=True", "subprocess shell=True（注入风险）"),
    (r"git\s+push\s+.*--force", "强制推送"),
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"\bsudo\b", "sudo 提权"),
    (r"while\s+True\s*:", "while True 无限循环（注意退出条件）"),
]


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

        # 1. AST 语法错误检测（tree-sitter 容错解析）
        tree = self._analyzer.parse(code, ext)
        if tree is not None:
            error_count = 0
            cursor = tree.walk()

            def count_errors(node) -> None:
                nonlocal error_count
                if node.is_error or node.type == "ERROR":
                    error_count += 1
                    line = node.start_point[0] + 1
                    snippet = code.split("\n")[line - 1][:120] if line <= len(code.split("\n")) else ""
                    findings.append(f"语法错误(行 {line}): {snippet}")
                for child in node.children:
                    count_errors(child)

            count_errors(tree.root_node)

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