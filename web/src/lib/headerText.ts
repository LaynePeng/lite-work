/** 解析 Header 文本（与 LLM 设置页同口径，另外兼容 JSON 与 curl -H）。
 *
 * 支持：
 *   - 每行 `Key: Value` 或 `Key=Value`（按**第一个**分隔符切分，**值可含冒号**）
 *   - `#` 开头的行忽略；空行忽略
 *   - curl 形式：`-H "Key: Value"` / `--header "Key: Value"`
 *   - 包裹引号与行尾逗号（`'Key' : 'Value' ,`）会被剥掉
 *   - JSON 对象：`{"Key":"Value"}`
 *
 * 返回 `null` 表示无法解析（调用方应提示用户并阻止保存，而不是静默丢弃）。
 */
export function parseHeaderText(text: string): Record<string, string> | null {
  const t = (text || "").trim();
  if (!t) return {};
  if (t.startsWith("{")) {
    try {
      const obj = JSON.parse(t);
      if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
      const out: Record<string, string> = {};
      for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
        out[String(k)] = String(v);
      }
      return out;
    } catch {
      return null;
    }
  }
  /** 去掉包裹引号与行尾逗号（key/value 各自处理；**先去尾逗号再去引号**） */
  const stripNoise = (s: string): string =>
    s.trim().replace(/[,;]+$/, "").trim().replace(/^['"]+/, "").replace(/['"]+$/, "").trim();

  const out: Record<string, string> = {};
  for (const rawLine of t.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    line = line.replace(/^-H\s*/i, "").replace(/^--header\s*/i, "");
    // 按第一个 : 或 = 切分 → 值里可以含冒号（如 Authorization: Bearer a:b）
    const m = line.match(/^([^:=]+)[:=](.*)$/);
    if (!m) return null;
    const key = stripNoise(m[1]);
    const value = stripNoise(m[2]);
    if (key) out[key] = value;
  }
  return Object.keys(out).length ? out : null;
}
