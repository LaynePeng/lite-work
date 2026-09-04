// 文件修改 diff 展示（opencode 风格）：文件路径 + 增删行数徽标 + 行级 patch 着色
import UnifiedDiff, { isUnifiedDiff } from "./UnifiedDiff";

// 判定结果是否为文件修改回执（含 diff 正文）
export function isFileDiff(text: string): boolean {
  return text.includes("[Patch Success]") && text.includes("\n@@");
}

// 从 patch diff 正文中提取纯 diff 部分（跳过 [Patch Success] 头）
function extractDiffBody(text: string): string {
  const idx = text.indexOf("\n@@");
  if (idx === -1) return text;
  return text.slice(idx + 1);
}

// 解析 "[Patch Success]: 已更新 <path> (+N -M)" → 文件徽标
export function DiffStats({ text }: { text: string }) {
  const m = text.match(/^\[Patch Success\]: 已更新 (.+?) \(\+(\d+) -(\d+)\)/);
  if (!m) return null;
  return (
    <div className="diff-stats">
      <span className="diff-file">📄 {m[1]}</span>
      <span className="diff-add">+{m[2]}</span>
      <span className="diff-del">−{m[3]}</span>
    </div>
  );
}

// 按行渲染 patch：+ 绿 / − 红 / @@ 高亮 / 文件头灰
export function DiffPre({ text }: { text: string }) {
  if (!isFileDiff(text)) return <pre className="tool-result">{text}</pre>;
  const body = extractDiffBody(text);
  if (!isUnifiedDiff(body)) return <pre className="tool-result">{text}</pre>;
  return (
    <div className="tool-result unified-diff-wrap">
      <UnifiedDiff diff={body} />
    </div>
  );
}