// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 设置页故障隔离：任一区块加载失败必须"局部可见"，不得让其它区块（尤其"模型"页）
// 整页空白。回归背景：原先 模型/MCP 同处一个 Promise.all + 静默 catch，
// 任一失败（如 MCP 状态挂掉）就让模型页 load 不出来且没有任何提示。

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsModal from "./SettingsModal";

const ok = (v: unknown) => vi.fn().mockResolvedValue(v);

vi.mock("../api", () => ({
  api: {
    skills: vi.fn(),
    plugins: vi.fn(),
    pluginsBuiltin: vi.fn(),
    pluginsCommunity: vi.fn(),
    agents: vi.fn(),
    agentsTools: vi.fn(),
    pricingStatus: vi.fn(),
    config: vi.fn(),
    collabModes: vi.fn(),
    llmProviders: vi.fn(),
    llmConfig: vi.fn(),
    llmModels: vi.fn(),
    mcpStatus: vi.fn(),
    readSkill: vi.fn(),
    createSkill: vi.fn(),
    updateSkill: vi.fn(),
    deleteSkill: vi.fn(),
    saveAgent: vi.fn(),
    deleteAgent: vi.fn(),
    deletePlugin: vi.fn(),
    updateMcpServers: vi.fn(),
    updateLLMConfig: vi.fn(),
    testLLM: vi.fn(),
    syncPricing: vi.fn(),
    cleanupSessions: vi.fn(),
    updateConfig: vi.fn(),
    startInstallPlugin: vi.fn(),
    startInstallSkill: vi.fn(),
    installJob: vi.fn(),
    installJobControl: vi.fn(),
  },
}));

import { api } from "../api";

const PROVIDERS = [
  { id: "deepseek", name: "DeepSeek", kind: "openai" as const, models: ["deepseek-chat"], default_base_url: "", has_key: true, model: "deepseek-chat" },
];

function primeAll() {
  const a = api as unknown as Record<string, ReturnType<typeof ok>>;
  a.skills.mockResolvedValue({ skills: [] });
  a.plugins.mockResolvedValue({ plugins: [] });
  a.pluginsBuiltin.mockResolvedValue({ plugins: [] });
  a.pluginsCommunity.mockResolvedValue({ plugins: [], skills: [] });
  a.agents.mockResolvedValue({ agents: [] });
  a.agentsTools.mockResolvedValue({ tools: [], use_all: false });
  a.pricingStatus.mockResolvedValue({
    models_dev: { cached: false, models: 0, age_seconds: null, stale: true },
    provider: { name: "pricing-plugin", version: "1.0.0", description: "官方定价", source: "builtin" },
    sources: [
      { id: "deepseek", label: "DeepSeek 官方定价", url: "https://example.com", cached: false, models: 0, age_seconds: null, stale: true, snapshot_date: "2026-09-17" },
    ],
  });
  a.config.mockResolvedValue({});
  a.collabModes.mockResolvedValue({ modes: [] });
  a.llmProviders.mockResolvedValue(PROVIDERS);
  a.llmConfig.mockResolvedValue({ active: "deepseek", providers: { deepseek: { model: "deepseek-chat", models: ["deepseek-chat"], api_key: "sk-x" } } });
  a.mcpStatus.mockResolvedValue({ servers: [] });
}

beforeEach(() => {
  vi.clearAllMocks();
  primeAll();
});

describe("SettingsModal · 故障隔离", () => {
  it("正常：模型页显示供应商", async () => {
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    expect(await screen.findByText("DeepSeek")).toBeTruthy();
  });

  it("模型配置加载失败：显示可见错误 + 重试按钮", async () => {
    (api.llmProviders as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText(/模型配置加载失败/)).toBeTruthy();
    });
    expect(screen.getByText("重试加载")).toBeTruthy();
  });

  it("MCP 状态失败不连累模型页（回归：原 Promise.all 会整组丢弃）", async () => {
    (api.mcpStatus as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("mcp down"));
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    // 模型页仍渲染出供应商
    expect(await screen.findByText("DeepSeek")).toBeTruthy();
    // 且没有把模型配置标成失败
    expect(screen.queryByText(/模型配置加载失败/)).toBeNull();
  });

  it("定价数据逐源同步：按顺序调用并逐行呈现结果（同步步骤可见）", async () => {
    const calls: string[] = [];
    (api.syncPricing as ReturnType<typeof vi.fn>).mockImplementation(async (source: string) => {
      calls.push(source);
      return { id: source, ok: true, models: source === "models_dev" ? 100 : 2 };
    });
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(await screen.findByText(/立即同步定价数据/));
    // 逐源顺序：models.dev → 插件声明的官方源
    await waitFor(() => {
      expect(calls).toEqual(["models_dev", "deepseek"]);
    });
    // 每步结果回显（哪一步成功、索引了多少模型）
    expect(await screen.findByText(/已更新 100 个模型/)).toBeTruthy();
    expect(await screen.findByText(/已更新 2 个模型/)).toBeTruthy();
  });

  it("定价数据过期：提示「建议同步」（非阻塞、不强制）", async () => {
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    expect((await screen.findAllByText(/建议同步/)).length).toBeGreaterThan(0);
  });

  it("价格检查过期窗口：回显配置值，可设为 0（永不过期）并保存", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({ pricing_check_ttl_days: 30 });
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);

    const input = await screen.findByTitle(/0 = 永不过期/) as HTMLInputElement;
    // 回显 config 里的 30 天（config 是异步拉的）
    await waitFor(() => expect(input.value).toBe("30"));

    // 改成 0（永不过期）→ 保存走 /api/config，并按新窗口重拉一次状态
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.click(input.parentElement!.querySelector("button")!);
    await waitFor(() => {
      expect(api.updateConfig).toHaveBeenCalledWith({ pricing_check_ttl_days: 0 });
    });
    expect(await screen.findByText("已保存 ✓")).toBeTruthy();
    // 保存后重拉状态：让「建议同步」标签按新窗口刷新
    await waitFor(() => expect(api.pricingStatus).toHaveBeenCalledTimes(2));
  });

  it("插件列表失败：可见错误 + 重试，且模型页不受影响", async () => {
    (api.plugins as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("plugin exploded"));
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    expect(await screen.findByText("DeepSeek")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText(/插件列表加载失败/)).toBeTruthy();
    });
  });
});

describe("SettingsModal · 拉取模型列表", () => {
  it("成功：拉取结果原样回填 textarea 并提示数量", async () => {
    // 初始模型列表为空，避免触发覆盖确认弹窗
    (api.llmConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      active: "deepseek",
      providers: { deepseek: { model: "deepseek-chat", models: [], api_key: "sk-x" } },
    });
    (api.llmModels as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, models: ["m-b", "m-a"], message: "",
    });
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(await screen.findByText("⟳ 拉取模型列表"));
    // testing-library 对多行 placeholder 做空白归一化（换行折叠为空格），用 \s+ 正则定位
    const ta = await screen.findByPlaceholderText(/deepseek-chat\s+deepseek-reasoner/) as HTMLTextAreaElement;
    await waitFor(() => {
      expect(ta.value).toBe("m-b\nm-a");
    });
    expect(await screen.findByText(/已拉取 2 个模型/)).toBeTruthy();
    // 请求带上当前编辑态（api_key/model 等未脱敏字段会作为 overrides 传给后端）
    expect((api.llmModels as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe("deepseek");
  });

  it("失败：显示可见错误且不清空已填写的模型列表", async () => {
    // 默认 primeAll：deepseek 已有 models: ["deepseek-chat"]
    (api.llmModels as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false, models: [], message: "HTTP 401",
    });
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(await screen.findByText("⟳ 拉取模型列表"));
    expect(await screen.findByText(/无法读取模型列表：HTTP 401/)).toBeTruthy();
    // 同上：多行 placeholder 用 \s+ 正则定位（空白归一化）
    const ta = screen.getByPlaceholderText(/deepseek-chat\s+deepseek-reasoner/) as HTMLTextAreaElement;
    expect(ta.value).toBe("deepseek-chat");
  });
});

describe("SettingsModal · 思考模式推理回传", () => {
  it("回显后端策略：mode 与三个名单（换行展示）", async () => {
    (api.llmConfig as ReturnType<typeof vi.fn>).mockResolvedValue({
      active: "deepseek",
      providers: { deepseek: { model: "deepseek-chat", models: [], api_key: "sk-x" } },
      reasoning_replay: {
        mode: "never",
        providers: ["custom_abc"],
        model_hints: ["mimo"],
        exclude_providers: ["glm"],
      },
    });
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    const sel = await screen.findByTestId("rr-mode") as HTMLSelectElement;
    expect(sel.value).toBe("never");
    expect(await screen.findByDisplayValue("custom_abc")).toBeTruthy();
    expect(await screen.findByDisplayValue("mimo")).toBeTruthy();
    expect(await screen.findByDisplayValue("glm")).toBeTruthy();
  });

  it("缺省（后端未配）回落到 auto 且名单为空", async () => {
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    const sel = await screen.findByTestId("rr-mode") as HTMLSelectElement;
    expect(sel.value).toBe("auto");
  });

  it("保存：走 LLM 专用通道，名单按行拆分并去掉空行", async () => {
    render(<SettingsModal onClose={() => {}} onSaved={() => {}} />);
    fireEvent.change(await screen.findByTestId("rr-mode"), { target: { value: "always" } });
    fireEvent.change(await screen.findByPlaceholderText("custom_1788492302820"), {
      target: { value: "custom_a\n\ncustom_b" },
    });
    fireEvent.change(await screen.findByPlaceholderText("mimo"), { target: { value: "mimo" } });
    fireEvent.change(await screen.findByPlaceholderText("glm"), { target: { value: "sglm" } });
    fireEvent.click(await screen.findByText("💾 保存"));

    await waitFor(() => {
      expect((api.updateLLMConfig as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0);
    });
    const call = (api.updateLLMConfig as ReturnType<typeof vi.fn>).mock.calls[0];
    // 第 2 个参数必须是 undefined（而非 {}）：后端把「providers 非 None」当作
    // 「UI 提交的完整列表」，传 {} 会把未列出的 custom_* 供应商全部删除。
    // 回归背景：曾传 {} 导致用户配置的 providers 被清空。
    expect(call[1]).toBeUndefined();
    expect(call[2]).toEqual({
      mode: "always",
      providers: ["custom_a", "custom_b"],
      model_hints: ["mimo"],
      exclude_providers: ["sglm"],
    });
  });
});

describe("SettingsModal · 代理出网面清单", () => {
  // 回归点：配图（media）走 skills 里的 requests 子进程，只能靠 HTTP_PROXY 注入。
  // 设置页的 scope 清单是**手写数组**，后端加 scope 时极易漏配 —— 漏了就意味着
  // 用户在界面上永远勾不到「PPT 配图走代理」。
  const src = readFileSync(resolve(process.cwd(), "src/components/SettingsModal.tsx"), "utf-8");

  it("七个出网面都在界面上（含 media = PPT 配图）", () => {
    const expected: [string, string][] = [
      ["llm", "LLM API 调用"],
      ["webfetch", "Agent 联网工具"],
      ["meta", "models.dev 元数据"],
      ["market", "插件/技能市场"],
      ["mcp", "MCP 子进程"],
      ["shell", "Agent 执行的命令"],
      ["media", "PPT 配图"],
    ];
    for (const [key, label] of expected) {
      expect(src, `缺少 scope ${key}`).toContain(`["${key}", "${label}`);
    }
  });

  it("media 的 tooltip 说明是环境变量注入（与后端实现一致）", () => {
    expect(src).toMatch(/key === "media"[\s\S]{0,120}HTTP_PROXY/);
  });
});
