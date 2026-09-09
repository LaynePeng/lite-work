# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""review_code 静态审查工具测试。

覆盖两个历史误报回归：
1. .tsx/.jsx 文件必须用 tree-sitter tsx 语法解析（含 JSX 标签），
   否则 <div>/<select> 等 JSX 会全部变成 ERROR 节点 → review_code 满屏「语法错误」误报。
2. 反模式 exec 正则需词边界，create_subprocess_exec( 不应命中「使用 exec」。
"""
from pathlib import Path

from litework.tools.review import ReviewTools

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
    assert any("语法错误" in finding for finding in findings), findings


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
