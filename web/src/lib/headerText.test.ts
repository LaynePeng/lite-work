import { describe, expect, it } from "vitest";

import { parseHeaderText } from "./headerText";

describe("parseHeaderText（Header 输入解析）", () => {
  it("每行 Key: Value（与 LLM 页同口径）", () => {
    expect(parseHeaderText("x-api-key: sk-abc\nX-Api-Version: 1")).toEqual({
      "x-api-key": "sk-abc",
      "X-Api-Version": "1",
    });
  });

  it("Key=Value 形式", () => {
    expect(parseHeaderText("A=1\nB=2")).toEqual({ A: "1", B: "2" });
  });

  it("值里可以含冒号（按第一个分隔符切分）", () => {
    expect(parseHeaderText("X-Title: My App: beta")).toEqual({ "X-Title": "My App: beta" });
    expect(parseHeaderText("Authorization: Bearer a:b:c")).toEqual({
      Authorization: "Bearer a:b:c",
    });
  });

  it("忽略 # 注释与空行", () => {
    expect(parseHeaderText("# 说明\n\nk: v\n# 又一行注释")).toEqual({ k: "v" });
  });

  it("curl 形式 -H \"k: v\"（多行）", () => {
    expect(parseHeaderText('-H "x-api-key: sk-abc"\n-H "X-Api-Version: 1"')).toEqual({
      "x-api-key": "sk-abc",
      "X-Api-Version": "1",
    });
  });

  it("带前后空格与包裹引号", () => {
    expect(parseHeaderText("  'x-api-key' : 'sk-abc' ,  ")).toEqual({ "x-api-key": "sk-abc" });
  });

  it("JSON 对象也认（向后兼容）", () => {
    expect(parseHeaderText('{"x-api-key":"sk-abc","n":1}')).toEqual({
      "x-api-key": "sk-abc",
      n: "1",
    });
  });

  it("空输入 = 空对象；无法解析 = null（调用方须拦保存）", () => {
    expect(parseHeaderText("")).toEqual({});
    expect(parseHeaderText("   ")).toEqual({});
    expect(parseHeaderText("这行没有分隔符")).toBeNull();
    expect(parseHeaderText('{"bad": ')).toBeNull();
  });
});
