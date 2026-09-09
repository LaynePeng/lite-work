// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

// 统一 diff 渲染（opencode / git 风格）：单列交错视图
// 上下文行正常、删除行红底、插入行绿底，带新旧行号与 hunk 分隔条
import { useMemo } from "react";

export interface UnifiedDiffLine {
  type: "meta" | "hunk" | "context" | "add" | "del";
  oldLine: number | null; // 删除/上下文行的旧文件行号
  newLine: number | null; // 插入/上下文行的新文件行号
  text: string;
}

// 解析 unified diff 文本 → 行数组（自动跳过 diff --git / index 头）
export function parseUnifiedDiff(diff: string): UnifiedDiffLine[] {
  const out: UnifiedDiffLine[] = [];
  const lines = diff.split("\n");
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;

  for (const raw of lines) {
    const line = raw;
    if (line.startsWith("diff --git") || line.startsWith("index ") || line === "") {
      if (!inHunk && line === "") continue; // 只跳过 hunk 外的空行
      continue;
    }
    if (line.startsWith("--- a/") || line.startsWith("+++ b/") || line.startsWith("--- ") || line.startsWith("+++ ")) {
      out.push({ type: "meta", oldLine: null, newLine: null, text: line });
      continue;
    }
    if (line.startsWith("@@")) {
      // @@ -a,b +c,d @@
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        oldLine = parseInt(m[1], 10);
        newLine = parseInt(m[2], 10);
      }
      inHunk = true;
      out.push({ type: "hunk", oldLine: null, newLine: null, text: line });
      continue;
    }
    if (!inHunk) continue;

    if (line.startsWith("-")) {
      out.push({ type: "del", oldLine: oldLine, newLine: null, text: line });
      oldLine += 1;
    } else if (line.startsWith("+")) {
      out.push({ type: "add", oldLine: null, newLine: newLine, text: line });
      newLine += 1;
    } else {
      out.push({ type: "context", oldLine: oldLine, newLine: newLine, text: line });
      oldLine += 1;
      newLine += 1;
    }
  }
  return out;
}

export function isUnifiedDiff(text: string): boolean {
  return text.includes("\n@@") || text.startsWith("@@");
}

export default function UnifiedDiff({ diff }: { diff: string }) {
  const lines = useMemo(() => parseUnifiedDiff(diff), [diff]);
  if (lines.length === 0) return null;

  // 计算每个 hunk 段的起始偏移（用于定位上下文行号）
  const rowLines = lines.map((l, i) => ({ ...l, idx: i }));

  return (
    <div className="unified-diff">
      <table className="unified-diff-table">
        <tbody>
          {rowLines.map((l) => (
            <tr key={l.idx} className={`udiff-row udiff-${l.type}`}>
              <td className="udiff-oldnum">{l.oldLine ?? ""}</td>
              <td className="udiff-newnum">{l.newLine ?? ""}</td>
              <td className="udiff-content">
                <span className="udiff-prefix">
                  {l.type === "add" ? "+" : l.type === "del" ? "-" : " "}
                </span>
                {l.text.startsWith("+") || l.text.startsWith("-")
                  ? l.text.slice(1)
                  : l.text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}