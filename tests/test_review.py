# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""review_code 静态审查工具测试。

覆盖历史误报回归：
1. .tsx/.jsx 文件必须用 tree-sitter tsx 语法解析（含 JSX 标签），
   否则 <div>/<select> 等 JSX 会全部变成 ERROR 节点 → review_code 满屏「语法错误」误报。
2. 反模式 exec 正则需词边界，create_subprocess_exec( 不应命中「使用 exec」；
   同理正则字面量的 /re/.exec( 也不能命中（"." 也是非词字符，\b 挡不住）。
3. tree-sitter 的容错恢复节点会横跨整个文件（起点停在行 1），且内部嵌套多个 ERROR
   ——直接逐条上报会「报行 1」+「同一行重复多条」，故需过滤恢复节点、按行去重、限流。
4. .py 也要真查语法（此前 tree-sitter 只认 TS/JS/Java/Go，Python 文件等于没检查）。
"""
import re
from pathlib import Path

from litework.tools.review import _MAX_PARSE_FINDINGS, ReviewTools

GOOD_TSX = '''import React from "react";

export default function Composer() {
  const ok = true;
  return (
    <div className="wrap">
      <select value="a">
        <option value="a">A</option>
      </select>
      {ok && <span>hi</span>}
    </div>
  );
}
'''


def _review(review_tools: ReviewTools, name: str, content: str) -> list[str]:
    path = Path(review_tools.workspace) / name
    path.write_text(content, encoding="utf-8")
    return review_tools._review_file(str(path))


def test_tsx_with_jsx_has_no_false_syntax_errors(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "Composer.tsx", GOOD_TSX)
    assert not any("语法错误" in finding for finding in findings), findings


def test_tsx_real_syntax_error_is_detected(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "Broken.tsx", "const x = <div> 未闭合的 JSX\n")
    # TS/TSX 统一用「解析告警」措辞（grammar 对较新语法会误判，不能断言成「语法错误」）
    assert any(("解析告警" in f or "语法错误" in f) for f in findings), findings


def test_jsx_extension_also_uses_tsx_grammar(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "App.jsx", '<div className="ok"><span>x</span></div>\n')
    assert not any("语法错误" in finding for finding in findings), findings


def test_subprocess_exec_not_reported_as_exec_usage(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(
        review_tools,
        "spawn.py",
        "proc = await asyncio.create_subprocess_exec('git', 'status')\n",
    )
    assert not any("使用 exec" in finding for finding in findings), findings


def test_real_exec_still_detected(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "evil.py", "exec('print(1)')\n")
    assert any("使用 exec" in finding for finding in findings), findings


def test_regex_exec_not_reported_as_exec_usage(tmp_path: Path):
    """正则字面量的 .exec( 不能命中「使用 exec」（SettingsModal 真实误报）。"""
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(
        review_tools,
        "parse.ts",
        'const m = /^v?(\\d+)\\.(\\d+)$/.exec((v || "").trim());\n',
    )
    assert not any("使用 exec" in finding for finding in findings), findings


def test_inline_import_type_does_not_flood_report(tmp_path: Path):
    """内联 import("...") 类型会让 tree-sitter 误判：必须整文件级过滤 + 去重 + 限流。

    真实案例：SettingsModal.tsx 因这种写法被报 84 条「语法错误」，其中一条指向
    第 1 行（恢复节点起点），行 397 重复 3 次。
    """
    review_tools = ReviewTools(str(tmp_path))
    lines = ['import { req } from "./http";', ""]
    for index in range(12):
        lines.append(f'const d{index} = req<{{ m: import("./t").M; b?: B }}>("x{index}");')
    lines.append("export const done = true;")
    findings = _review(review_tools, "Many.tsx", "\n".join(lines) + "\n")

    parse_findings = [f for f in findings if "解析告警" in f or "语法错误" in f]
    # 1) TS/TSX 用降级措辞，不再断言成「语法错误」
    assert not any("语法错误" in f for f in findings), findings
    # 2) 首行是合法 import，绝不能被报（恢复节点已被过滤）
    assert not any("(行 1)" in f for f in parse_findings), parse_findings
    # 3) 按行去重：同一行不出现两次
    numbers = [int(re.search(r"行 (\d+)", f).group(1)) for f in parse_findings if "行 " in f]
    assert len(numbers) == len(set(numbers)), parse_findings
    # 4) 限流：逐条列出的不超过上限（其余折叠成汇总）
    listed = [f for f in parse_findings if f.startswith("解析告警(行")]
    assert len(listed) <= _MAX_PARSE_FINDINGS, parse_findings
    assert len(parse_findings) <= _MAX_PARSE_FINDINGS + 2, parse_findings
    # 5) 附「以 tsc 为准」提示，避免读者误信
    assert any(f.startswith("提示：") for f in findings), findings


def test_python_syntax_error_detected(tmp_path: Path):
    """Python 也要真查语法（此前 .py 完全没检查——tree-sitter 不认 Python）。"""
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "broken.py", "def f(:\n    pass\n")
    assert any("语法错误" in f for f in findings), findings


def test_valid_python_has_no_syntax_error(tmp_path: Path):
    review_tools = ReviewTools(str(tmp_path))
    findings = _review(review_tools, "ok.py", "def f() -> int:\n    return 1\n")
    assert not any("语法错误" in f for f in findings), findings
