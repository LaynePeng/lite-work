"""工具输出截断器（对应课程第2课 + 第4/5课，参考 OpenCode 实现）。

第一版的做法是「头尾各保留一半字符」。它有两个硬伤：
1. 按字符数截断对 Code Agent 输出很不合理 —— 输出是「行」组织的，
   按行截断才对；且一刀切头尾各半，恰恰丢掉了中间信息密度最高的部分；
2. 截断就是「丢弃」，模型永远看不到被删掉的内容，信息直接丢失。

本版改进（对齐 OpenCode 的 `Truncate.output`）：
1. 以「行 + 字节」双上限为准（默认 2000 行 / 50KB），而不是纯字符数；
2. 默认保留「头部」(direction="head")——命令回显、文件名、错误上下文通常
   都在输出开头，信息密度最高；tail 模式供脚本化输出使用；
3. 超限时把**完整输出落盘**到 truncation 目录，并返回「截断预览 + 磁盘路径 +
   如何按需读取」的提示 —— 上下文里只放一个轻量句柄，而不是 50KB 原文。
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
RETENTION_SECONDS = 7 * 24 * 3600  # 落盘文件保留 7 天


@dataclass
class TruncationResult:
    """截断结果：上下文里放 content，完整输出落在 output_path。"""
    content: str
    truncated: bool
    output_path: Optional[str] = None


def _count_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def truncate_tool_output(
    output: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    direction: str = "head",
    output_dir: Optional[str] = None,
) -> TruncationResult:
    """OpenCode 风格的工具输出截断。

    参数：
        output:     工具原始输出
        max_lines:  行数上限（默认 2000）
        max_bytes:  字节数上限（默认 50KB）
        direction:  "head"（保留开头）或 "tail"（保留结尾）
        output_dir: 落盘目录；为 None 时不落盘，仅返回截断后的行

    返回：
        TruncationResult，truncated=False 表示未超限，content 即原样输出；
        超限时 content 为「截断预览 + 完整路径提示」，完整内容落在 output_path。
    """
    if not output:
        return TruncationResult(content=output, truncated=False)

    lines = output.split("\n")
    total_bytes = _count_bytes(output)

    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(content=output, truncated=False)

    out: list[str] = []
    bytes_used = 0
    hit_bytes = False

    if direction == "head":
        for i in range(min(len(lines), max_lines)):
            size = _count_bytes(lines[i]) + (1 if i > 0 else 0)
            if bytes_used + size > max_bytes:
                hit_bytes = True
                break
            out.append(lines[i])
            bytes_used += size
    else:  # tail
        picked: list[str] = []
        for i in range(len(lines) - 1, -1, -1):
            if len(picked) >= max_lines:
                break
            size = _count_bytes(lines[i]) + (1 if picked else 0)
            if bytes_used + size > max_bytes:
                hit_bytes = True
                break
            picked.append(lines[i])
            bytes_used += size
        out = picked[::-1]

    removed = total_bytes - bytes_used if hit_bytes else len(lines) - len(out)
    unit = "bytes" if hit_bytes else "lines"

    preview = "\n".join(out)

    if output_dir:
        path = _write_full_output(output_dir, output)
        hint = (
            f"\n\nThe tool call succeeded but the output was truncated "
            f"({removed} {unit} omitted). Full output saved to: {path}\n"
            f"Use the read_file/search tools to inspect it on demand. "
            f"Do NOT read the full file unless you need to — save context."
        )
        content = f"{preview}\n\n...{removed} {unit} truncated...{hint}"
    else:
        content = f"{preview}\n\n...{removed} {unit} truncated (no output file saved)..."
    return TruncationResult(content=content, truncated=True, output_path=path if output_dir else None)


def _write_full_output(output_dir: str, text: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    _cleanup_expired(output_dir)
    name = f"tool_{int(time.time())}_{uuid.uuid4().hex[:8]}.txt"
    path = os.path.join(output_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _cleanup_expired(output_dir: str) -> None:
    """删除超过保留期的落盘文件，避免无限膨胀。"""
    cutoff = time.time() - RETENTION_SECONDS
    try:
        for entry in os.listdir(output_dir):
            if not entry.startswith("tool_"):
                continue
            fp = os.path.join(output_dir, entry)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass
