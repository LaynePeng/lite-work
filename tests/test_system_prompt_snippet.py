# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""System Prompt 工具清单的一行摘要（snippet）回归测试。

背景：tool schema 已经把完整 description 投喂给模型，System Prompt 的工具清单
若再贴一遍全文，同一段文字每次请求都要付两遍钱。这里锁定三件事：
1. 工具清单只用一行摘要，不含完整 description；
2. 摘要本身可读（非空、不超长、不复述参数细节）；
3. 摘要不影响 tool schema —— 发给 LLM 的 function 定义仍是完整 description，
   且不夹带 ToolDefinition 的内部字段（OpenAI 会因未知键报 400）。
"""
from __future__ import annotations

from litework.core.system_prompt import (
    SNIPPET_MAX_CHARS,
    TOOL_SNIPPETS,
    SystemPromptBuilder,
    tool_snippet,
)
from litework.core.types import ToolDefinition
from litework.llm.openai_compat import OpenAICompatAdapter


def _tool(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=description,
                          parameters={"type": "object", "properties": {}})


# ---------------------------------------------------------------- tool_snippet

def test_snippet_keeps_first_sentence():
    t = _tool("read_file", "读取指定文件内容，支持带行号与行范围。以下是详细的参数说明……")
    assert tool_snippet(t) == "读取指定文件内容，支持带行号与行范围。"


def test_snippet_truncates_long_description():
    t = _tool("huge", "很长的说明" * 100)
    s = tool_snippet(t)
    assert len(s) <= SNIPPET_MAX_CHARS + 1        # +1：省略号
    assert s.endswith("…")


def test_snippet_never_returns_empty():
    assert tool_snippet(_tool("blank", "")) == "—"
    assert tool_snippet(_tool("ws", "   \n\t  ")) == "—"


def test_snippet_strips_markdown_noise():
    t = _tool("x", "**加粗标题**：`代码` 与 > 引用")
    assert "*" not in tool_snippet(t)
    assert "`" not in tool_snippet(t)


def test_override_table_wins():
    """人工摘要优先于自动摘要（自动摘不准时用 TOOL_SNIPPETS 兜）。"""
    for name, expect in TOOL_SNIPPETS.items():
        assert tool_snippet(_tool(name, "完全不同的描述。")) == expect


# ---------------------------------------------------------------- 接入 System Prompt

def test_prompt_tool_list_uses_snippet_not_full_description(tmp_path):
    """工具清单里不应出现完整 description —— 那部分是 schema 的活。"""
    long_desc = "这是一个非常长的工具描述，包含大量参数细节，" * 20 + "结尾标记ENDMARK"
    tools = [_tool("long_tool", long_desc)]
    prompt = SystemPromptBuilder.build(str(tmp_path), tools)
    assert "**long_tool**" in prompt
    assert "结尾标记ENDMARK" not in prompt          # 细节只留在 schema
    assert tool_snippet(tools[0]) in prompt


def test_prompt_shrinks_vs_full_description(tmp_path):
    """同批工具下，工具清单段明显短于「贴全文」的写法。"""
    tools = [_tool(f"t{i}", "描述文字" * 40) for i in range(20)]
    prompt = SystemPromptBuilder.build(str(tmp_path), tools)
    # 只比工具清单段：prompt 其余部分（技能索引等）与本改动无关
    start = prompt.index("### 可用工具")
    end = prompt.index("### 工作规则")
    section = prompt[start:end]
    full = "\n".join(f"- **{t.name}**: {t.description}" for t in tools)
    assert len(section) < len(full) * 0.5
    # 每个工具行都受上限约束（"- **name**: " 前缀之外不超过 SNIPPET_MAX_CHARS + 省略号）
    for t, line in zip(tools, (l for l in section.splitlines() if l.startswith("- **"))):
        body = line[len(f"- **{t.name}**: "):]
        assert len(body) <= SNIPPET_MAX_CHARS + 1


# ---------------------------------------------------------------- schema 完整性

def test_openai_schema_keeps_full_description_and_no_internal_fields():
    """发给 LLM 的 function schema：完整 description + 只有三个标准键。"""
    long_desc = "完整描述" * 100
    adapter = OpenAICompatAdapter(api_key="k", model="m", base_url="http://localhost")
    payload = adapter._build_payload([], [_tool("t", long_desc)])
    fn = payload["tools"][0]["function"]
    assert fn["description"] == long_desc            # schema 用全文，不缩水
    assert set(fn) == {"name", "description", "parameters"}
