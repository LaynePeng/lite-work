// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { useMemo } from "react";
import type { TabItem } from "../types";
import UnifiedDiff, { parseUnifiedDiff } from "./UnifiedDiff";

export default function FileViewer({ tab }: { tab: TabItem }) {
  const lines = useMemo(() => (tab.fileContent ?? "").split("\n"), [tab.fileContent]);
  const diffLines = useMemo(() => tab.fileDiff ? parseUnifiedDiff(tab.fileDiff) : [], [tab.fileDiff]);
  const hasDiff = diffLines.length > 0;

  // 统计增删行数
  const stats = useMemo(() => {
    let adds = 0, dels = 0;
    for (const l of diffLines) {
      if (l.type === "add") adds++;
      if (l.type === "del") dels++;
    }
    return { adds, dels };
  }, [diffLines]);

  return (
    <div className="file-viewer">
      <div className="file-viewer-header">
        <span className="file-viewer-path">{tab.filePath}</span>
        {tab.fileLanguage && <span className="file-viewer-lang">{tab.fileLanguage}</span>}
        {hasDiff ? (
          <>
            <span className="diff-add">+{stats.adds}</span>
            <span className="diff-del">−{stats.dels}</span>
          </>
        ) : (
          <span className="file-viewer-lines">{lines.length} 行</span>
        )}
      </div>

      {hasDiff ? (
        <div className="file-content">
          <UnifiedDiff diff={tab.fileDiff!} />
        </div>
      ) : (
        <div className="file-content">
          <table className="file-code-table">
            <tbody>
              {lines.map((line, i) => (
                <tr key={i} className="code-line">
                  <td className="code-line-num">{i + 1}</td>
                  <td className="code-line-content">{line || " "}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}