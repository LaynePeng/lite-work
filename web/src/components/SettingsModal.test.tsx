// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 设置页故障隔离：任一区块加载失败必须"局部可见"，不得让其它区块（尤其"模型"页）
// 整页空白。回归背景：原先 模型/MCP 同处一个 Promise.all + 静默 catch，
// 任一失败（如 MCP 状态挂掉）就让模型页 load 不出来且没有任何提示。

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
