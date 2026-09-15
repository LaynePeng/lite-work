// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

/**
 * 文件打开目标判定（文件树 / 产出物面板共用）。
 *
 * 规则（用户确认的固定行为，无设置开关）：
 * - markdown（.md/.markdown）**优先系统默认程序**（Electron shell.openPath），
 *   系统无关联程序时由调用方**静默回退**内置查看器/预览；浏览器模式直接内置。
 * - 其余文本/代码类 → 内置查看器；非文本类 → 桌面端系统打开 / 浏览器提示不支持。
 */

/** 代码/文本类扩展名：内置查看器可渲染（markdown 也在其中，供回退路径使用）。 */
export const TEXT_LIKE_EXT = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp",
  ".css", ".scss", ".less", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".toml",
  ".md", ".markdown", ".txt", ".log", ".sh", ".bash", ".zsh", ".sql", ".rb", ".swift",
  ".kt", ".svelte", ".vue", ".astro", ".ini", ".cfg", ".conf", ".env", ".gitignore",
  ".puml", ".plantuml", ".mmd", ".mermaid", ".csv", ".tsv",
]);

/** 优先用系统默认程序打开的扩展名（失败静默回退内置）。 */
export const SYSTEM_FIRST_EXT = new Set([".md", ".markdown"]);

export type OpenTarget = "system" | "builtin" | "unsupported";

export function extOf(path: string): string {
  const dot = path.lastIndexOf(".");
  // 无点或以点开头（隐藏文件如 .gitignore）视为无扩展名
  return dot > 0 ? path.slice(dot).toLowerCase() : "";
}

export function isTextLikePath(path: string): boolean {
  const ext = extOf(path);
  return TEXT_LIKE_EXT.has(ext) || ext === "";
}

export interface ResolveOpenOptions {
  /** 桌面桥接是否存在（Electron 环境，window.liteWork.openFile） */
  hasBridge: boolean;
  /** 强制使用内置（右键「用内置查看」），绕过系统优先规则 */
  forceBuiltin?: boolean;
}

/**
 * 解析打开目标：
 * - "system"：调系统默认程序（bridge.openFile；markdown 失败时调用方应静默回退 builtin）
 * - "builtin"：内置查看器/预览
 * - "unsupported"：非文本类且浏览器模式（无 bridge），调用方提示下载
 */
export function resolveOpenTarget(path: string, opts: ResolveOpenOptions): OpenTarget {
  if (opts.forceBuiltin) return "builtin";
  const ext = extOf(path);
  if (SYSTEM_FIRST_EXT.has(ext)) {
    return opts.hasBridge ? "system" : "builtin";
  }
  if (TEXT_LIKE_EXT.has(ext) || ext === "") {
    return "builtin";
  }
  return opts.hasBridge ? "system" : "unsupported";
}
