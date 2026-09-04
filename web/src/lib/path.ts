/**
 * 跨平台路径工具：兼容 Windows（\）与 POSIX（/）分隔符。
 *
 * 后端返回的绝对路径是原生分隔符（Windows 为 \），而工作区内相对路径
 * 约定统一用 /；这里集中处理两种风格，避免各处手写 split("/")。
 */

/** 取路径最后一段（文件名/目录名），兼容 / 与 \。 */
export function baseName(p: string): string {
  if (!p) return "";
  const normalized = p.replace(/[\\/]+$/, "");
  const idx = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return idx >= 0 ? normalized.slice(idx + 1) : normalized;
}

/**
 * 把路径拆成面包屑分段：Windows 盘符（C:\）单独一段，其余按 / 或 \ 切分。
 * 例："C:\Users\proj" → ["C:", "Users", "proj"]；"/Users/proj" → ["Users", "proj"]
 */
export function pathSegments(p: string): string[] {
  if (!p) return [];
  // Windows 盘符根（C:\ 或 C:/）单独作为一段，便于点击切换回盘符根
  const driveMatch = p.match(/^([A-Za-z]:)[\\/]/);
  if (driveMatch) {
    const rest = p.slice(driveMatch[0].length);
    const segs = rest.split(/[\\/]+/).filter(Boolean);
    return [driveMatch[1], ...segs];
  }
  return p.split(/[\\/]+/).filter(Boolean);
}

/** 判断是否为 Windows 盘符路径（如 C:\ 或 C:/）。 */
export function isDriveRoot(p: string): boolean {
  return /^[A-Za-z]:[\\/]?$/.test(p.trim());
}
