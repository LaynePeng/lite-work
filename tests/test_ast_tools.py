# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

from pathlib import Path

import pytest

from litework.tools.ast_tools import ASTAnalyzer, ASTTools


PYTHON_SOURCE = '''class Greeter:
    def greet(self, name: str) -> str:
        message = f"Hello, {name}"
        return message

async def chat(prompt: str) -> str:
    answer = prompt.upper()
    return answer

def helper() -> int:
    return 42
'''


def test_python_outline_contains_classes_functions_and_methods():
    symbols = ASTAnalyzer().extract_outline(PYTHON_SOURCE, ".py")
    summary = {(symbol.kind, symbol.name) for symbol in symbols}

    assert ("class", "Greeter") in summary
    assert ("method", "greet") in summary
    assert ("function", "chat") in summary
    assert ("function", "helper") in summary


def test_python_focused_symbol_keeps_target_and_hides_other_bodies():
    result = ASTAnalyzer().generate_skeleton(PYTHON_SOURCE, ".py", "chat")

    assert 'answer = prompt.upper()' in result
    assert 'message = f"Hello, {name}"' not in result
    assert "return 42" not in result
    assert result.count("body omitted for context saving") == 2


def test_python_focused_symbol_preserves_enclosing_function():
    source = '''def create_app():
    def helper():
        return "hidden"

    async def chat(prompt):
        return prompt

    return chat
'''

    result = ASTAnalyzer().generate_skeleton(source, ".py", "chat")

    assert "def create_app():" in result
    assert "async def chat(prompt):" in result
    assert "return prompt" in result
    assert 'return "hidden"' not in result


@pytest.mark.asyncio
async def test_python_ast_tools_execute(tmp_path: Path):
    source = tmp_path / "sample.py"
    source.write_text(PYTHON_SOURCE, encoding="utf-8")
    tools = ASTTools(str(tmp_path))

    outline = await tools.execute("get_file_outline", {"filePath": "sample.py"})
    focused = await tools.execute(
        "read_focused_symbol",
        {"filePath": "sample.py", "symbolName": "chat"},
    )
    missing = await tools.execute(
        "read_focused_symbol",
        {"filePath": "sample.py", "symbolName": "missing"},
    )

    assert "[CLASS] Greeter" in outline
    assert "[METHOD] greet" in outline
    assert "[FUNCTION] chat" in outline
    assert 'symbol "chat"' in focused
    assert "answer = prompt.upper()" in focused
    assert "未找到函数/方法" in missing


@pytest.mark.asyncio
async def test_python_ast_syntax_error_is_reported(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    tools = ASTTools(str(tmp_path))

    result = await tools.execute("get_file_outline", {"filePath": "broken.py"})

    assert "语法解析失败" in result


@pytest.mark.parametrize(
    ("ext", "source", "expected"),
    [
        (".java", "class Demo { void chat() { return; } }", ("method", "chat")),
        (".go", "package demo\nfunc Chat() { }", ("function", "Chat")),
    ],
)
def test_java_and_go_outline(ext, source, expected):
    symbols = ASTAnalyzer().extract_outline(source, ext)
    assert (expected[0], expected[1]) in {(item.kind, item.name) for item in symbols}


@pytest.mark.parametrize(
    ("ext", "source", "target", "hidden"),
    [
        (".java", "class Demo { void chat() { return; } void helper() { int x = 1; } }", "chat", "int x = 1"),
        (".go", "package demo\nfunc Chat() { }\nfunc Helper() { println(1) }", "Chat", "println(1)"),
    ],
)
def test_java_and_go_focused_symbol(ext, source, target, hidden):
    result = ASTAnalyzer().generate_skeleton(source, ext, target)
    assert target in result
    assert hidden not in result
