# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""精确代码编辑工具（对应课程第7课（安全代码操作））：

1. apply_search_replace - Search-and-Replace 块匹配器（精确匹配 → 模糊行匹配 + 缩进保持）
2. apply_unified_diff   - Unified Diff 补丁应用器（锚点自适应偏移）

失败时返回详细错误，促使 Agent 重新 read_file 自愈。
"""
from __future__ import annotations

import difflib
import os
from typing import Any, Dict, List, Tuple

from ..core.types import ToolDefinition


# ---------------------------------------------------------------- BlockReplacer


class BlockReplacer:
    """Search-and-Replace 块替换，精确匹配失败后自动模糊行匹配并保持缩进。"""

    def replace_block(self, source_code: str, search: str, replace: str) -> Tuple[bool, str, str]:
        # 1. 精确匹配
        if search in source_code:
            return True, source_code.replace(search, replace, 1), ""

        trimmed = search.strip()
        if not trimmed:
            return False, source_code, "搜索块为空。"

        # 2. 模糊匹配（逐行 trim 后匹配）
        source_lines = source_code.split("\n")
        search_lines = [l.strip() for l in search.split("\n")]
        match_start = -1
        for i in range(len(source_lines) - len(search_lines) + 1):
            ok = True
            for j, sl in enumerate(search_lines):
                if source_lines[i + j].strip() != sl:
                    ok = False
                    break
            if ok:
                match_start = i
                break

        if match_start == -1:
            return False, source_code, "在目标文件中未找到精确或模糊匹配的 <SEARCH> 块。"

        match_end = match_start + len(search_lines)
        indent = source_lines[match_start][: len(source_lines[match_start]) - len(source_lines[match_start].lstrip())]
        indented_replace = "\n".join(
            line if idx == 0 else indent + line.lstrip()
            for idx, line in enumerate(replace.split("\n"))
        )
        new_lines = source_lines[:match_start] + indented_replace.split("\n") + source_lines[match_end:]
        return True, "\n".join(new_lines), ""


# ---------------------------------------------------------------- DiffPatcher


class DiffPatcher:
    """解析简化版 Unified Diff（@@ -oldStart,oldLen +newStart,newLen @@），带锚点偏移修正。"""

    def apply_patch(self, source_code: str, patch_str: str) -> Tuple[bool, str, str]:
        lines = source_code.split("\n")
        patch_lines = patch_str.split("\n")
        i = 0
        header_re = r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"

        import re

        while i < len(patch_lines):
            m = re.match(header_re, patch_lines[i])
            if not m:
                i += 1
                continue

            expected_old_start = int(m.group(1)) - 1
            i += 1
            old_lines: List[str] = []
            new_lines: List[str] = []
            while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                p = patch_lines[i]
                if p.startswith(" "):
                    old_lines.append(p[1:])
                    new_lines.append(p[1:])
                elif p.startswith("-"):
                    old_lines.append(p[1:])
                elif p.startswith("+"):
                    new_lines.append(p[1:])
                i += 1

            actual_start = self._find_anchor(lines, old_lines, expected_old_start)
            if actual_start == -1:
                return False, source_code, f"无法在 {expected_old_start + 1} 行附近定位上下文锚点。"
            lines[actual_start : actual_start + len(old_lines)] = new_lines

        return True, "\n".join(lines), ""

    @staticmethod
    def _find_anchor(source_lines: List[str], target_old_lines: List[str], hint: int) -> int:
        if not target_old_lines:
            return hint
        first = target_old_lines[0].strip()
        for offset in range(16):
            for idx in (hint + offset, hint - offset):
                if 0 <= idx < len(source_lines) and source_lines[idx].strip() == first:
                    return idx
        return -1


# ---------------------------------------------------------------- EditorTools


class EditorTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self._replacer = BlockReplacer()
        self._patcher = DiffPatcher()

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="apply_search_replace",
                description="通过 SEARCH/REPLACE 块精确更新文件代码（缩进需完全一致；模糊匹配失败会返回原因）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "文件相对路径"},
                        "searchBlock": {"type": "string", "description": "被替换的完整原始代码片段（含原缩进）"},
                        "replaceBlock": {"type": "string", "description": "写入的新代码片段"},
                        "then": {
                            "type": "array", "items": {"type": "string"},
                            "description": "可选：主操作成功后立即执行的验证命令（最多 4 条，如跑测试/lint），结果附在本次输出里，省一轮请求",
                        },
                    },
                    "required": ["filePath", "searchBlock", "replaceBlock"],
                },
            ),
            ToolDefinition(
                name="edit_files",
                description="批量精确编辑（复合工具 ★）：一次调用完成多个文件的多个补丁，"
                            "多文件重构/成组修改用它，比逐个 apply_search_replace 少 N-1 轮。"
                            "先全部校验（search 块存在性）再依次应用，某条失败默认停止（stop_on_error）",
                parameters={
                    "type": "object",
                    "properties": {
                        "edits": {
                            "type": "array",
                            "description": "编辑条目列表（建议 ≤10 条；同文件多条按序应用）",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "filePath": {"type": "string", "description": "文件相对路径"},
                                    "mode": {"type": "string", "enum": ["search_replace", "unified_diff"],
                                             "description": "补丁格式，默认 search_replace"},
                                    "searchBlock": {"type": "string", "description": "mode=search_replace：被替换的完整原始代码（含原缩进）"},
                                    "replaceBlock": {"type": "string", "description": "mode=search_replace：新代码片段"},
                                    "diff": {"type": "string", "description": "mode=unified_diff：Unified Diff 补丁文本"},
                                },
                                "required": ["filePath"],
                            },
                        },
                        "stop_on_error": {"type": "boolean", "default": True,
                                          "description": "某条失败即停止后续（默认 true，保证原子性弱化语义）"},
                    },
                    "required": ["edits"],
                },
            ),
            ToolDefinition(
                name="apply_unified_diff",
                description="应用标准 Unified Diff 补丁（@@ -oldStart,oldLen +newStart,newLen @@ 格式）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "文件相对路径"},
                        "diff": {"type": "string", "description": "Unified Diff 补丁文本"},
                        "then": {
                            "type": "array", "items": {"type": "string"},
                            "description": "可选：主操作成功后立即执行的验证命令（最多 4 条，如跑测试/lint），结果附在本次输出里，省一轮请求",
                        },
                    },
                    "required": ["filePath", "diff"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "edit_files":
            return self._edit_files(args)
        rel_path = args.get("filePath", "")
        raw = os.path.expanduser(rel_path)
        full_path = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        inside = full_path == self.workspace or full_path.startswith(self.workspace + os.sep)
        if not inside and args.get("_approved_external_access") != "write":
            return "[Security Violation]: 项目外写入未获授权"
        if not os.path.exists(full_path):
            return f"[Edit Error]: 文件不存在: {rel_path}"

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()

        if name == "apply_search_replace":
            ok, result, reason = self._replacer.replace_block(
                source, args.get("searchBlock", ""), args.get("replaceBlock", "")
            )
            if not ok:
                return (f"[Patch Failed]: {reason}\n"
                        f"建议：请重新 read_file 获取目标区域的最新精确内容与格式后重试。")
        elif name == "apply_unified_diff":
            ok, result, reason = self._patcher.apply_patch(source, args.get("diff", ""))
            if not ok:
                return f"[Patch Failed]: {reason}\n建议：请重新 read_file 获取精确上下文后重试。"
        else:
            raise ValueError(f"Unknown Editor Tool: {name}")

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(result)
        return self._diff_summary(rel_path, source, result)

    def _diff_summary(self, rel_path: str, source: str, result: str) -> str:
        """返回带文件路径与增删行数的结果（+N -M），并附 Unified Diff 供自检。"""
        diff = list(difflib.unified_diff(
            source.splitlines(), result.splitlines(),
            fromfile=rel_path, tofile=rel_path, lineterm="",
        ))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        head = f"[Patch Success]: 已更新 {rel_path} (+{added} -{removed})"
        if not diff:
            return head
        body = "\n".join(diff)
        if len(body) > 4000:
            body = body[:4000] + "\n...(diff 过长已截断)"
        return f"{head}\n\n{body}"

    # ------------------------------------------------------------ 复合工具

    _EDIT_FILES_MAX = 20  # 单次条目上限（防误用巨列表爆上下文）

    def _edit_files(self, args: Dict[str, Any]) -> str:
        """批量编辑：先逐条校验（读文件 + 补丁试算），全部通过才落盘。

        两阶段的意义：避免「前 3 条已写盘、第 4 条失败」留下半截状态——
        校验失败时磁盘零改动，调用方修好补丁重试即可（与单条工具的
        失败语义一致）。同文件多条按序应用（后一条的 search 针对前一条
        的结果），因此校验也串行模拟执行。
        """
        edits = args.get("edits") or []
        if not isinstance(edits, list) or not edits:
            return "[Edit Files Error]: edits 不能为空"
        if len(edits) > self._EDIT_FILES_MAX:
            return f"[Edit Files Error]: 单次最多 {self._EDIT_FILES_MAX} 条（本次 {len(edits)} 条）"
        stop_on_error = bool(args.get("stop_on_error", True))
        access = args.get("_approved_external_access", "")

        # 阶段一：校验 + 试算（内存态，不落盘）
        pending: List[Tuple[str, str, str]] = []  # (rel_path, full_path, new_content)
        sources: Dict[str, str] = {}
        errors: List[str] = []
        for i, edit in enumerate(edits, 1):
            rel_path = str(edit.get("filePath") or "")
            if not rel_path:
                errors.append(f"#{i}: filePath 为空")
                if stop_on_error:
                    break
                continue
            raw = os.path.expanduser(rel_path)
            full_path = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
            inside = full_path == self.workspace or full_path.startswith(self.workspace + os.sep)
            if not inside and access != "write":
                errors.append(f"#{i} {rel_path}: 项目外写入未获授权")
                if stop_on_error:
                    break
                continue
            if full_path not in sources:
                if not os.path.exists(full_path):
                    errors.append(f"#{i} {rel_path}: 文件不存在")
                    if stop_on_error:
                        break
                    continue
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    sources[full_path] = f.read()
            source = sources[full_path]
            mode = str(edit.get("mode") or "search_replace")
            if mode == "search_replace":
                ok, result, reason = self._replacer.replace_block(
                    source, str(edit.get("searchBlock") or ""), str(edit.get("replaceBlock") or ""))
            elif mode == "unified_diff":
                ok, result, reason = self._patcher.apply_patch(source, str(edit.get("diff") or ""))
            else:
                ok, result, reason = False, "", f"未知 mode: {mode}"
            if not ok:
                errors.append(f"#{i} {rel_path}: {reason}")
                if stop_on_error:
                    break
                continue
            sources[full_path] = result
            pending.append((rel_path, full_path, result))

        # 阶段二：全过才落盘（stop_on_error 模式下有错则零落盘）
        if errors and stop_on_error:
            return ("[Edit Files Aborted]: 校验失败，磁盘未改动（重试请修正以下条目）\n"
                    + "\n".join(f"- {e}" for e in errors))
        written = 0
        summary_lines: List[str] = []
        for rel_path, full_path, content in pending:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            written += 1
            summary_lines.append(f"[OK] {rel_path}")
        head = f"[Edit Files Success]: {written}/{len(edits)} 条已应用"
        if errors:
            head += f"（{len(errors)} 条失败被跳过：stop_on_error=false）"
        if not pending:
            return "[Edit Files Error]: 没有可应用的条目\n" + "\n".join(f"- {e}" for e in errors)
        return head + "\n" + "\n".join(summary_lines) + (
            "\n失败明细:\n" + "\n".join(f"- {e}" for e in errors) if errors else "")
