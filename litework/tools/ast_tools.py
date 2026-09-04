"""AST 语义工具（对应课程第6课（代码理解））：Tree-sitter 符号大纲提取 + 骨架上下文压缩。"""
from __future__ import annotations

import asyncio
import ast
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import tree_sitter
import tree_sitter_go
import tree_sitter_java
import tree_sitter_typescript

from ..core.types import ToolDefinition

_LANG_CACHE: Dict[str, tree_sitter.Language] = {}


def _get_language(ext: str) -> Optional[tree_sitter.Language]:
    if ext in _LANG_CACHE:
        return _LANG_CACHE[ext]
    if ext in (".ts", ".js", ".mjs", ".cjs"):
        # 纯 TS/JS 语法（TS 是 JS 超集，可解析两者）
        lang = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    elif ext in (".tsx", ".jsx"):
        # TSX/JSX 必须用 tsx 语法（含 JSX/XML 标签），否则 <div> 等标签会全部变成 ERROR 节点
        lang = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    elif ext == ".java":
        lang = tree_sitter.Language(tree_sitter_java.language())
    elif ext == ".go":
        lang = tree_sitter.Language(tree_sitter_go.language())
    else:
        return None
    _LANG_CACHE[ext] = lang
    return lang


@dataclass
class SymbolOutline:
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str


class ASTAnalyzer:
    """解析 Python、TS、JS、Java、Go 文件提取符号大纲。"""

    _FUNCTION_NODES = {"function_declaration", "method_definition", "method_declaration",
                       "arrow_function", "function_expression", "generator_function_declaration"}

    def parse(self, code: str, ext: str) -> Optional[tree_sitter.Tree]:
        lang = _get_language(ext)
        if lang is None:
            return None
        parser = tree_sitter.Parser(lang)
        return parser.parse(code.encode("utf-8"))

    def extract_outline(self, code: str, ext: str) -> List[SymbolOutline]:
        if ext == ".py":
            return self._extract_python_outline(code)
        tree = self.parse(code, ext)
        if tree is None:
            return []
        symbols: List[SymbolOutline] = []
        code_bytes = code.encode("utf-8")

        def visit(node: tree_sitter.Node) -> None:
            node_type = node.type
            kind = None

            if node_type in ("function_declaration", "generator_function_declaration"):
                kind = "function"
            elif node_type in ("class_declaration", "class_specifier"):
                kind = "class"
            elif node_type == "interface_declaration":
                kind = "interface"
            elif node_type in ("method_definition", "method_declaration"):
                kind = "method"
            elif node_type == "type_spec":
                kind = "type"
            elif node_type == "lexical_declaration":
                # const foo = () => {}
                var_name = node.child_by_field_name("declarator")
                if var_name and var_name.type == "variable_declarator":
                    value = var_name.child_by_field_name("value")
                    if value and value.type in ("arrow_function", "function_expression"):
                        name_node = var_name.child_by_field_name("name")
                        if name_node:
                            symbols.append(SymbolOutline(
                                name=name_node.text.decode("utf-8", "replace"),
                                kind="function",
                                start_line=node.start_point[0] + 1,
                                end_line=node.end_point[0] + 1,
                                signature=name_node.text.decode("utf-8", "replace"),
                            ))

            if kind is not None:
                name_node = node.child_by_field_name("name")
                name = name_node.text.decode("utf-8", "replace") if name_node else "<anonymous>"
                body_start = node.start_byte
                first_line_end = code_bytes.find(b"\n", body_start)
                first_line = code_bytes[body_start:first_line_end if first_line_end != -1 else node.end_byte]
                symbols.append(SymbolOutline(
                    name=name,
                    kind=kind,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=first_line.decode("utf-8", "replace").strip(),
                ))

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return symbols

    @staticmethod
    def _extract_python_outline(code: str) -> List[SymbolOutline]:
        tree = ast.parse(code)
        lines = code.splitlines()
        symbols: List[SymbolOutline] = []
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if isinstance(parents.get(node), ast.ClassDef) else "function"
                name = node.name
            elif isinstance(node, ast.ClassDef):
                kind = "class"
                name = node.name
            else:
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            signature = lines[start - 1].strip() if lines else name
            symbols.append(SymbolOutline(name, kind, start, end, signature))
        symbols.sort(key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.name))
        return symbols

    def generate_skeleton(self, code: str, ext: str, target_symbol: str) -> str:
        """骨架抽取：只保留目标符号的完整函数体，其余函数体替换为占位注释。"""
        if ext == ".py":
            return self._generate_python_skeleton(code, target_symbol)
        tree = self.parse(code, ext)
        if tree is None:
            return code

        code_bytes = code.encode("utf-8")
        replacements: List[List[int]] = []  # [start, end]

        def walk(node: tree_sitter.Node) -> None:
            node_type = node.type
            is_callable = node_type in self._FUNCTION_NODES
            if is_callable:
                name_node = node.child_by_field_name("name")
                name = name_node.text.decode("utf-8", "replace") if name_node else None
                body = node.child_by_field_name("body")
                if name and body and name != target_symbol:
                    replacements.append([body.start_byte, body.end_byte])
            for child in node.children:
                walk(child)

        walk(tree.root_node)

        result = code
        for start, end in sorted(replacements, reverse=True):
            result = result[:start] + "{ /* ... body omitted for context saving ... */ }" + result[end:]
        return result

    @staticmethod
    def _generate_python_skeleton(code: str, target_symbol: str) -> str:
        tree = ast.parse(code)
        encoded = code.encode("utf-8")
        line_starts = [0]
        for index, byte in enumerate(encoded):
            if byte == 10:
                line_starts.append(index + 1)

        def offset(line: int, column: int) -> int:
            return line_starts[line - 1] + column

        callable_types = (ast.FunctionDef, ast.AsyncFunctionDef)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        targets = {
            node for node in ast.walk(tree)
            if isinstance(node, callable_types) and node.name == target_symbol
        }
        preserved = set(targets)
        for target in targets:
            parent = parents.get(target)
            while parent is not None:
                if isinstance(parent, callable_types):
                    preserved.add(parent)
                parent = parents.get(parent)

        replacements: List[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, callable_types) or node in preserved or not node.body:
                continue
            parent = parents.get(node)
            if any(
                isinstance(ancestor, callable_types) and ancestor not in preserved
                for ancestor in ASTAnalyzer._ancestors(parent, parents)
            ):
                continue
            first = node.body[0]
            start = offset(first.lineno, first.col_offset)
            end = offset(getattr(node, "end_lineno", first.lineno), getattr(node, "end_col_offset", 0))
            replacements.append((start, end, "pass  # ... body omitted for context saving ..."))

        result = encoded
        for start, end, replacement in sorted(replacements, reverse=True):
            result = result[:start] + replacement.encode("utf-8") + result[end:]
        return result.decode("utf-8", "replace")

    @staticmethod
    def _ancestors(node: Optional[ast.AST], parents: Dict[ast.AST, ast.AST]):
        while node is not None:
            yield node
            node = parents.get(node)


_analyzer = ASTAnalyzer()


class ASTTools:
    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="get_file_outline",
                description="通过 AST 解析获取文件的结构大纲（支持 Python、TS、JS、Java、Go 的函数/类/方法列表与行号签名）",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "文件相对路径（支持 .py/.ts/.tsx/.js/.java/.go）"},
                    },
                    "required": ["filePath"],
                },
            ),
            ToolDefinition(
                name="read_focused_symbol",
                description="针对大型文件仅提取目标函数/方法的骨架上下文，其余函数体隐藏，支持 Python、TS、JS、Java、Go",
                parameters={
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string", "description": "文件相对路径"},
                        "symbolName": {"type": "string", "description": "需要聚焦查看的函数/方法名"},
                    },
                    "required": ["filePath", "symbolName"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        rel_path = args.get("filePath", "")
        raw = os.path.expanduser(rel_path)
        full_path = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        inside = full_path == self.workspace or full_path.startswith(self.workspace + os.sep)
        if not inside and args.get("_approved_external_access") != "read":
            return "[Security Violation]: 项目外读取未获授权"
        if not os.path.exists(full_path):
            return f"[Error]: 文件不存在: {rel_path}"
        ext = os.path.splitext(full_path)[1].lower()
        if ext not in (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".go"):
            return f"[Error]: 暂不支持解析 {ext or '无扩展名'} 文件（支持 Python/TS/JS/Java/Go）"

        code = await asyncio.to_thread(self._read, full_path)

        if name == "get_file_outline":
            try:
                symbols = await asyncio.to_thread(_analyzer.extract_outline, code, ext)
            except SyntaxError as exc:
                return f"[Error]: {rel_path} 语法解析失败（行 {exc.lineno}: {exc.msg}）"
            if not symbols:
                return f"在 {rel_path} 中未发现顶层函数/类/接口符号。"
            summary = "\n".join(
                f"[{s.kind.upper()}] {s.name} (行 {s.start_line}-{s.end_line}) -> {s.signature}"
                for s in symbols
            )
            return f"{rel_path} 的符号大纲 ({len(symbols)} 个):\n{summary}"

        if name == "read_focused_symbol":
            symbol = args.get("symbolName", "")
            try:
                symbols = await asyncio.to_thread(_analyzer.extract_outline, code, ext)
                if not any(item.name == symbol and item.kind in ("function", "method") for item in symbols):
                    return f'[Error]: 在 {rel_path} 中未找到函数/方法 symbol "{symbol}"'
                skeleton = await asyncio.to_thread(_analyzer.generate_skeleton, code, ext, symbol)
            except SyntaxError as exc:
                return f"[Error]: {rel_path} 语法解析失败（行 {exc.lineno}: {exc.msg}）"
            return f'聚焦视图 symbol "{symbol}" @ {rel_path}:\n\n{skeleton}'

        raise ValueError(f"Unknown AST Tool: {name}")

    @staticmethod
    def _read(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
