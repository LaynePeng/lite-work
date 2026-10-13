// 富块导出：纯函数部分（文件名 / CSV / HTML 转义 / 表格 HTML 安全）。
import { describe, expect, it } from "vitest";

import {
  buildCsv, buildExportHtml, csvField, escapeHtml, exportBaseName,
  safeFileName, stripExt,
} from "./richExport";

describe("richExport 文件名规整", () => {
  it("safeFileName 去掉路径分隔符与非法字符，压缩空白", () => {
    expect(safeFileName("a/b\\c:d*e?f\"g<h>i|j")).toBe("a b c d e f g h i j");
    expect(safeFileName("  多余   空格  ")).toBe("多余 空格");
    expect(safeFileName("")).toBe("export");
    expect(safeFileName("...hidden")).toBe("hidden");
  });

  it("stripExt 只去最后一段扩展名（隐藏文件不动）", () => {
    expect(stripExt("report.final.png")).toBe("report.final");
    expect(stripExt(".gitignore")).toBe(".gitignore");
    expect(stripExt("noext")).toBe("noext");
  });

  it("exportBaseName 用标题，缺标题用兜底名", () => {
    expect(exportBaseName("各方案月成本", "chart")).toBe("各方案月成本");
    expect(exportBaseName(undefined, "chart")).toBe("chart");
    expect(exportBaseName("   ", "table")).toBe("table");
    // 非法字符被规整（不产生路径分隔符）
    expect(exportBaseName("a/b:c", "chart")).toBe("a b c");
  });

  it("safeFileName 不残留可用于目录穿越的点段", () => {
    // 分隔符先被替换，首尾点被剥离 → 不产生 ".." 目录段
    const out = safeFileName("../../etc/passwd");
    expect(out).not.toContain("..");
    expect(out).not.toContain("/");
    expect(out.startsWith(".")).toBe(false);
  });
});

describe("richExport CSV", () => {
  it("csvField 按 RFC 4180 转义", () => {
    expect(csvField("plain")).toBe("plain");
    expect(csvField("a,b")).toBe('"a,b"');
    expect(csvField('say "hi"')).toBe('"say ""hi"""');
    expect(csvField("line1\nline2")).toBe('"line1\nline2"');
  });

  it("buildCsv 前置 BOM + CRLF，数值列原样", () => {
    const csv = buildCsv({ columns: ["名称", "值"], rows: [["A", "1"], ["含,逗号", "2"]] });
    expect(csv.startsWith("\uFEFF")).toBe(true);
    expect(csv).toContain("名称,值\r\n");
    expect(csv).toContain('"含,逗号",2\r\n');
  });

  it("buildCsv 容忍行短于表头（补空串）", () => {
    const csv = buildCsv({ columns: ["a", "b", "c"], rows: [["1"]] });
    expect(csv).toContain("1,,\r\n");
  });
});

describe("richExport HTML", () => {
  it("escapeHtml 转义五个危险字符", () => {
    expect(escapeHtml('<script>"x"&y</script>')).toBe(
      "&lt;script&gt;&quot;x&quot;&amp;y&lt;/script&gt;",
    );
    expect(escapeHtml("")).toBe("");
  });

  it("buildExportHtml 产出自足文档：内联 SVG + 主题变量 + 数据表", () => {
    const host = document.createElement("div");
    host.innerHTML = '<svg viewBox="0 0 100 50"><rect width="10" height="10" fill="var(--accent)"/></svg>';
    const html = buildExportHtml({
      title: "销量趋势",
      wrap: host,
      filename: "sales",
      table: { columns: ["月", "值"], rows: [["一月", "3"]], numeric: [1] },
    });
    expect(html).toContain("<!DOCTYPE html>");
    expect(html).toContain("<title>销量趋势</title>");
    expect(html).toContain("销量趋势");
    expect(html).toContain("<rect");
    expect(html).toContain("<summary>数据表（1 行）</summary>");
    expect(html).toContain('class="num"');
    // 自足：不引用任何外部资源
    expect(html).not.toMatch(/<(script|link)[^>]+src=/i);
    expect(html).not.toContain('href="http');
  });

  it("buildExportHtml 把 var(...) 解成字面量色（脱离应用后仍可见）", () => {
    const host = document.createElement("div");
    host.innerHTML = '<svg viewBox="0 0 10 10"><rect width="4" height="4" fill="var(--accent)"/></svg>';
    const html = buildExportHtml({ title: "t", wrap: host, filename: "f" });
    expect(html).not.toContain("var(--accent)");
    expect(html).toContain("fill=");
  });

  it("buildExportHtml 标题被转义（防注入）", () => {
    const host = document.createElement("div");
    host.innerHTML = '<svg viewBox="0 0 10 10"></svg>';
    const html = buildExportHtml({ title: '<img src=x onerror=alert(1)>', wrap: host, filename: "f" });
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("&lt;img");
  });

  it("buildExportHtml 无 SVG 时抛错（调用方据此提示）", () => {
    const host = document.createElement("div");
    expect(() => buildExportHtml({ wrap: host, filename: "f" })).toThrow(/未找到/);
  });
});
