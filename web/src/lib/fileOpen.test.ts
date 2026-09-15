// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//

import { describe, expect, it } from "vitest";
import { extOf, isTextLikePath, resolveOpenTarget, SYSTEM_FIRST_EXT } from "./fileOpen";

describe("extOf / isTextLikePath", () => {
  it("提取扩展名（大小写不敏感；隐藏文件视为无扩展名）", () => {
    expect(extOf("README.md")).toBe(".md");
    expect(extOf("A.DOCX")).toBe(".docx");
    expect(extOf("Makefile")).toBe("");
    expect(extOf(".gitignore")).toBe("");
  });

  it("文本类判定", () => {
    expect(isTextLikePath("README.md")).toBe(true);
    expect(isTextLikePath("main.py")).toBe(true);
    expect(isTextLikePath("Makefile")).toBe(true);
    expect(isTextLikePath("a.docx")).toBe(false);
  });
});

describe("resolveOpenTarget（markdown 优先系统默认程序）", () => {
  it("markdown + 桌面端 → system", () => {
    expect(resolveOpenTarget("docs/README.md", { hasBridge: true })).toBe("system");
    expect(resolveOpenTarget("notes.markdown", { hasBridge: true })).toBe("system");
  });

  it("markdown + 浏览器（无桥接）→ builtin", () => {
    expect(resolveOpenTarget("docs/README.md", { hasBridge: false })).toBe("builtin");
  });

  it("markdown + forceBuiltin（右键「用内置查看」）→ builtin", () => {
    expect(resolveOpenTarget("docs/README.md", { hasBridge: true, forceBuiltin: true })).toBe("builtin");
  });

  it("其余文本/代码类 → builtin（与是否桌面无关）", () => {
    expect(resolveOpenTarget("src/main.py", { hasBridge: true })).toBe("builtin");
    expect(resolveOpenTarget("src/main.py", { hasBridge: false })).toBe("builtin");
    expect(resolveOpenTarget("Makefile", { hasBridge: true })).toBe("builtin");
  });

  it("非文本类：桌面 → system；浏览器 → unsupported", () => {
    expect(resolveOpenTarget("产出物/方案.docx", { hasBridge: true })).toBe("system");
    expect(resolveOpenTarget("产出物/方案.docx", { hasBridge: false })).toBe("unsupported");
  });

  it("SYSTEM_FIRST_EXT 只含 markdown", () => {
    expect([...SYSTEM_FIRST_EXT].sort()).toEqual([".markdown", ".md"]);
  });
});
