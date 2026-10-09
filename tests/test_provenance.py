# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""不可信内容防护测试（W6）。

覆盖：
1. 包裹格式：开标签/闭标签/提示规则/幂等（不重复包裹）；
2. 来源判断：webfetch/mcp_* 是外部，read_file 不是；
3. 升级判断：同一轮"外部内容+高危动作"→ true；无外部 → false；只读工具 → false；
4. 来源提取与审批提示；
5. 集成：webfetch 结果确实被包裹（通过 wrap_untrusted 间接验证）。
"""
from __future__ import annotations

from litework.core.provenance import (
    contains_untrusted,
    extract_untrusted_sources,
    is_external,
    provenance_note,
    should_escalate,
    wrap_untrusted,
)


# ---------------------------------------------------------------- 包裹格式

def test_wrap_basic():
    wrapped = wrap_untrusted("Hello from the web", source="web", url="https://example.com")
    assert wrapped.startswith('<untrusted source="web"')
    assert "Hello from the web" in wrapped
    assert "</untrusted>" in wrapped  # 闭标签存在
    # 提示规则在闭标签之后
    assert wrapped.index("</untrusted>") < wrapped.index("[注意]")


def test_wrap_idempotent():
    once = wrap_untrusted("content", source="web")
    twice = wrap_untrusted(once, source="web")
    assert once == twice  # 不重复包裹


def test_wrap_empty():
    assert wrap_untrusted("") == ""


def test_wrap_url_truncated():
    long_url = "https://example.com/" + "x" * 500
    wrapped = wrap_untrusted("content", source="web", url=long_url)
    assert len(wrapped) < len(long_url)  # URL 被截断


# ---------------------------------------------------------------- 来源判断

def test_is_external():
    assert is_external("webfetch")
    assert is_external("webfetch_batch")
    assert is_external("mcp_some_tool")
    assert not is_external("read_file")
    assert not is_external("write_file")
    assert not is_external("execute_command")


# ---------------------------------------------------------------- 升级判断

def test_should_escalate_external_plus_high_risk():
    assert should_escalate(True, "write_file")
    assert should_escalate(True, "execute_command")
    assert should_escalate(True, "delete_file")
    assert should_escalate(True, "git_push")


def test_should_not_escalate_no_external():
    assert not should_escalate(False, "write_file")
    assert not should_escalate(False, "execute_command")


def test_should_not_escalate_readonly_tool():
    assert not should_escalate(True, "read_file")
    assert not should_escalate(True, "search_code")
    assert not should_escalate(True, "get_file_outline")


def test_should_escalate_mcp_tool():
    """MCP 工具（未知风险面）也升级。"""
    assert should_escalate(True, "mcp_unknown_tool")


# ---------------------------------------------------------------- 来源提取与提示

def test_extract_sources():
    text = 'some text <untrusted source="web">content</untrusted> more <untrusted source="mcp">data</untrusted>'
    sources = extract_untrusted_sources(text)
    assert set(sources) == {"web", "mcp"}


def test_extract_sources_empty():
    assert extract_untrusted_sources("no tags here") == []
    assert extract_untrusted_sources("") == []


def test_provenance_note():
    note = provenance_note(["web"])
    assert "网页" in note
    assert "外部" in note
    assert provenance_note([]) == ""


def test_contains_untrusted():
    assert contains_untrusted('<untrusted source="web">x</untrusted>')
    assert not contains_untrusted("normal text")
    assert not contains_untrusted("")
