import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { LLMProviderMeta, LLMProviderSettings, MCPServerConfig, MCPServerStatus, SkillInfo } from "../types";

export default function SettingsModal({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [providers, setProviders] = useState<LLMProviderMeta[]>([]);
  const [activeProvider, setActiveProvider] = useState("");
  const [editing, setEditing] = useState<Record<string, Partial<LLMProviderSettings>>>({});
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<"llm" | "mcp" | "skills" | "general">("llm");
  // 综合设置：孤儿会话清理
  const [cleaning, setCleaning] = useState(false);
  const [cleanResult, setCleanResult] = useState<{ ok: boolean; text: string } | null>(null);
  // 综合设置：zip 大小上限（MB）
  const [maxZipSize, setMaxZipSize] = useState<number>(20);
  const [zipSaved, setZipSaved] = useState(false);
  // Skills triggers 匹配模式
  const [triggerMode, setTriggerMode] = useState<"substring" | "advanced">("substring");
  const [triggerModeSaved, setTriggerModeSaved] = useState(false);

  // Skills 管理（独立于 LLM/MCP 配置，操作即时生效）
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillContent, setSkillContent] = useState<{ name: string; content: string } | null>(null);
  const [skillBusy, setSkillBusy] = useState(false);
  const [skillMsg, setSkillMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [newSkill, setNewSkill] = useState({ name: "", description: "" });
  // 技能权限规则（glob → allow/deny/ask），保存走 /api/config
  const [permRules, setPermRules] = useState<Array<{ pattern: string; action: "allow" | "deny" | "ask" }>>([]);
  const [permDirty, setPermDirty] = useState(false);

  const refreshSkills = useCallback(() => {
    api.skills().then((r) => setSkills(r.skills)).catch(() => setSkills([]));
  }, []);

  useEffect(() => {
    refreshSkills();
    // 拉取技能权限规则（config.json 的 skill_permissions）与综合设置项
    api.config().then((c) => {
      const rules = c.skill_permissions || {};
      setPermRules(Object.entries(rules).map(([pattern, action]) => ({
        pattern, action: (action as "allow" | "deny" | "ask") || "allow",
      })));
      setPermDirty(false);
      if (typeof c.max_zip_size_mb === "number" && c.max_zip_size_mb > 0) {
        setMaxZipSize(c.max_zip_size_mb);
      }
      if (c.skill_trigger_mode === "substring" || c.skill_trigger_mode === "advanced") {
        setTriggerMode(c.skill_trigger_mode);
      }
    }).catch(() => { /* 配置拉取失败不阻塞技能页 */ });
  }, [refreshSkills]);

  const savePermRules = useCallback(async () => {
    const rules: Record<string, "allow" | "deny" | "ask"> = {};
    for (const r of permRules) {
      const p = r.pattern.trim().toLowerCase();
      if (p) rules[p] = r.action;
    }
    await api.updateConfig({ skill_permissions: rules });
    setPermRules(Object.entries(rules).map(([pattern, action]) => ({ pattern, action })));
    setPermDirty(false);
    setSkillMsg({ ok: true, text: "技能权限规则已保存并即时生效" });
    refreshSkills();
  }, [permRules, refreshSkills]);

  // ------------------------------------------------------------ Skills 操作

  const skillAction = useCallback(async (fn: () => Promise<string>) => {
    setSkillBusy(true);
    setSkillMsg(null);
    try {
      const text = await fn();
      setSkillMsg({ ok: true, text });
      refreshSkills();
    } catch (e) {
      setSkillMsg({ ok: false, text: (e as Error).message });
    } finally {
      setSkillBusy(false);
    }
  }, [refreshSkills]);

  const handleImport = (source?: string, zipBase64?: string) => {
    const scope = window.prompt("导入到哪个范围？输入 workspace（需已打开项目）或 user", "workspace");
    if (!scope) return;
    void skillAction(async () => {
      const r = await api.importSkill({ source, zip_base64: zipBase64, scope, name: undefined });
      return `已导入: ${r.skills.map((s) => s.name).join(", ")}`;
    });
  };

  const handleZipFile = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = (reader.result as string).split(",")[1] ?? "";
      handleImport(undefined, base64);
    };
    reader.readAsDataURL(file);
  };

  // MCP 服务器配置（独立于 LLM 配置保存，保存即热重连）
  const [mcpServers, setMcpServers] = useState<Record<string, MCPServerConfig>>({});
  const [mcpArgsText, setMcpArgsText] = useState<Record<string, string>>({});
  const [mcpStatus, setMcpStatus] = useState<MCPServerStatus[]>([]);
  const [mcpSaving, setMcpSaving] = useState(false);
  const [mcpResult, setMcpResult] = useState<string | null>(null);
  const [reasoningOpen, setReasoningOpen] = useState(false);

  // 自定义 Header 的编辑文本（每行 "Key: Value" 或 "Key=Value"），blur 时解析提交
  const [headersText, setHeadersText] = useState<Record<string, string>>({});

  useEffect(() => {
    Promise.all([api.llmProviders(), api.llmConfig(), api.mcpStatus()]).then(([p, c, m]) => {
      setProviders(p);
      setActiveProvider(c.active);
      // 初始化编辑状态
      const edit: Record<string, Partial<LLMProviderSettings>> = {};
      for (const [pid, s] of Object.entries(c.providers)) {
        edit[pid] = { ...s };
      }
      setEditing(edit);
      // MCP：用运行状态初始化可编辑配置
      setMcpStatus(m.servers || []);
      const cfg: Record<string, MCPServerConfig> = {};
      for (const s of m.servers || []) {
        cfg[s.name] = { command: s.command, args: s.args, enabled: s.enabled };
      }
      setMcpServers(cfg);
      // 自定义 Header：dict → 每行 "Key: Value" 的可编辑文本
      const texts: Record<string, string> = {};
      for (const [pid, s] of Object.entries(c.providers)) {
        const headers = (s as LLMProviderSettings).custom_headers || {};
        texts[pid] = Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join("\n");
      }
      setHeadersText(texts);
    }).catch(() => {});
  }, []);

  // ------------------------------------------------------------ MCP 编辑

  const updateMcp = (name: string, patch: Partial<MCPServerConfig>) => {
    setMcpServers((prev) => ({ ...prev, [name]: { ...(prev[name] || { command: "" }), ...patch } }));
  };

  const addMcpServer = () => {
    const name = `server_${Date.now().toString().slice(-6)}`;
    setMcpServers((prev) => ({ ...prev, [name]: { command: "", args: [], enabled: true } }));
  };

  const removeMcpServer = (name: string) => {
    setMcpServers((prev) => {
      const copy = { ...prev };
      delete copy[name];
      return copy;
    });
  };

  const [renamingMcp, setRenamingMcp] = useState<{ from: string; value: string } | null>(null);

  const renameMcpServer = (from: string, to: string) => {
    const newName = to.trim().replace(/\s+/g, "-");
    setRenamingMcp(null);
    if (!newName || newName === from) return;
    if (newName in mcpServers) {
      window.alert(`已存在同名服务器「${newName}」`);
      return;
    }
    // 保持原有键顺序，仅替换键名（保存时全量替换重连，工具前缀 mcp_<name>_ 随之更新）
    setMcpServers((prev) => {
      const next: Record<string, MCPServerConfig> = {};
      for (const [k, v] of Object.entries(prev)) next[k === from ? newName : k] = v;
      return next;
    });
    // 参数草稿的键也要跟着迁移
    setMcpArgsText((prev) => {
      if (!(from in prev)) return prev;
      const copy = { ...prev };
      copy[newName] = copy[from];
      delete copy[from];
      return copy;
    });
  };

  const saveMcp = useCallback(async () => {
    setMcpSaving(true);
    setMcpResult(null);
    try {
      const payload: Record<string, MCPServerConfig> = {};
      for (const [name, s] of Object.entries(mcpServers)) {
        const command = (s.command || "").trim();
        if (!command) continue; // 空 command 的条目不保存
        payload[name] = {
          command,
          args: Array.isArray(s.args) ? s.args : String(s.args || "").split(/\s+/).filter(Boolean),
          enabled: s.enabled !== false,
        };
      }
      const res = await api.updateMcpServers(payload);
      setMcpStatus(res.servers || []);
      setMcpResult("已保存并重连（新任务生效，MCP 工具调用需审批）");
      onSaved();
    } catch (err) {
      setMcpResult(`保存失败: ${(err as Error).message}`);
    } finally {
      setMcpSaving(false);
    }
  }, [mcpServers, onSaved]);

  const update = (pid: string, key: string, value: string | string[] | number | boolean | null | Record<string, string>) => {
    setEditing((e) => ({ ...e, [pid]: { ...(e[pid] || {}), [key]: value } }));
  };

  // 自定义 Header 文本 → dict：每行按第一个冒号/等号切分（值可含冒号，如 URL），
  // 空行、# 注释、无分隔符的行忽略；同键后者覆盖。
  const parseHeaders = (text: string): Record<string, string> => {
    const headers: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const sepIdx = Math.min(
        ...[trimmed.indexOf(":"), trimmed.indexOf("=")].filter((i) => i > 0).concat([Infinity]),
      );
      if (!Number.isFinite(sepIdx)) continue;
      const key = trimmed.slice(0, sepIdx).trim();
      const value = trimmed.slice(sepIdx + 1).trim();
      if (key && value) headers[key] = value;
    }
    return headers;
  };

  const commitHeaders = (pid: string, text: string) => {
    update(pid, "custom_headers", parseHeaders(text));
  };

  const providerMeta = providers.find((p) => p.id === activeProvider);
  const currentEdit = editing[activeProvider] || {};
  const availableModels = (currentEdit.models as string[] | undefined) ?? providerMeta?.models ?? [];
  const isCustom = activeProvider.startsWith("custom_");

  // 推理强度中文标签
  const reasoningLabel = (v?: string) => {
    switch (v) {
      case "low": return "低";
      case "medium": return "中";
      case "high": return "高";
      case "max": return "最大";
      default: return "关";
    }
  };
  const reasoningSupported = providerMeta?.reasoning_supported ?? false;
  const reasoningModels = providerMeta?.reasoning_models ?? [];

  const addCustomProvider = () => {
    const id = `custom_${Date.now()}`;
    const next: LLMProviderSettings = {
      api_key: "", has_key: false, base_url: "", model: "", models: [],
      temperature: 0.2, reasoning_effort: "", name: "自定义供应商", custom_headers: {},
    };
    setProviders((prev) => [...prev, {
      id, name: next.name || id, kind: "openai", models: [], default_base_url: "",
      has_key: false, model: "",
    }]);
    setEditing((prev) => ({ ...prev, [id]: next }));
    setActiveProvider(id);
  };

  const removeCustomProvider = () => {
    if (!isCustom) return;
    const next = providers.filter((p) => p.id !== activeProvider);
    setProviders(next);
    setEditing((prev) => { const copy = { ...prev }; delete copy[activeProvider]; return copy; });
    setActiveProvider(next[0]?.id || "deepseek");
  };

  const handleTest = useCallback(async () => {
    if (!activeProvider) return;
    setTesting(true);
    setTestResult(null);
    try {
      const overrides: Record<string, unknown> = {};
      const e = editing[activeProvider];
      if (e?.api_key && !e.api_key.includes("…") && e.api_key !== "****") overrides.api_key = e.api_key;
      if (e?.base_url) overrides.base_url = e.base_url;
      if (e?.model) overrides.model = e.model;
      if (e?.temperature) overrides.temperature = e.temperature;
      if (e?.reasoning_effort) overrides.reasoning_effort = e.reasoning_effort;
      // 测试连接带上当前编辑的自定义 Header（含未 blur 的文本框内容）
      const liveText = headersText[activeProvider];
      if (liveText !== undefined) {
        overrides.custom_headers = parseHeaders(liveText);
      } else if (e?.custom_headers !== undefined) {
        overrides.custom_headers = e.custom_headers;
      }
      const res = await api.testLLM(activeProvider, Object.keys(overrides).length ? overrides : undefined);
      setTestResult(res);
    } catch (err) {
      setTestResult({ ok: false, message: (err as Error).message });
    } finally {
      setTesting(false);
    }
  }, [activeProvider, editing, headersText]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const providersPayload: Record<string, Partial<LLMProviderSettings>> = {};
      for (const pid of Object.keys(editing)) {
        providersPayload[pid] = { ...editing[pid] };
        if (headersText[pid] !== undefined) {
          providersPayload[pid].custom_headers = parseHeaders(headersText[pid]);
        }
      }
      await api.updateLLMConfig(activeProvider, providersPayload);
      setTestResult({ ok: true, message: "配置已保存" });
      onSaved();
    } catch (err) {
      setTestResult({ ok: false, message: (err as Error).message });
    } finally {
      setSaving(false);
    }
  }, [activeProvider, editing, onSaved]);

  // 一键清理孤儿会话：删除所有关联项目目录已不存在的会话
  const handleCleanup = useCallback(async () => {
    if (!window.confirm("将删除所有关联项目目录已不存在的会话，删除后无法恢复。确认继续？")) return;
    setCleaning(true);
    setCleanResult(null);
    try {
      const res = await api.cleanupSessions();
      setCleanResult({ ok: true, text: `已清理 ${res.deleted} 个无法关联项目的会话` });
      onSaved();
    } catch (err) {
      setCleanResult({ ok: false, text: `清理失败: ${(err as Error).message}` });
    } finally {
      setCleaning(false);
    }
  }, [onSaved]);

  // 保存 zip 大小上限
  const saveZipSize = useCallback(async () => {
    setZipSaved(false);
    try {
      await api.updateConfig({ max_zip_size_mb: maxZipSize });
      setZipSaved(true);
      setTimeout(() => setZipSaved(false), 2000);
      onSaved();
    } catch (err) {
      window.alert(`保存失败: ${(err as Error).message}`);
    }
  }, [maxZipSize, onSaved]);

  // 保存 triggers 匹配模式
  const saveTriggerMode = useCallback(async () => {
    setTriggerModeSaved(false);
    try {
      await api.updateConfig({ skill_trigger_mode: triggerMode });
      setTriggerModeSaved(true);
      setTimeout(() => setTriggerModeSaved(false), 2000);
      onSaved();
    } catch (err) {
      window.alert(`保存失败: ${(err as Error).message}`);
    }
  }, [triggerMode, onSaved]);

  return (
      <div className="modal-overlay">
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>⚙️ 设置</h2>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          <div className="settings-tabs">
            <button
              className={`settings-tab ${activeTab === "llm" ? "active" : ""}`}
              onClick={() => setActiveTab("llm")}
            >
              LLM
            </button>
            <button
              className={`settings-tab ${activeTab === "mcp" ? "active" : ""}`}
              onClick={() => setActiveTab("mcp")}
            >
              MCP Server
            </button>
            <button
              className={`settings-tab ${activeTab === "skills" ? "active" : ""}`}
              onClick={() => setActiveTab("skills")}
            >
              Skills
            </button>
            <button
              className={`settings-tab ${activeTab === "general" ? "active" : ""}`}
              onClick={() => setActiveTab("general")}
            >
              综合设置
            </button>
          </div>

          {activeTab === "llm" ? (
            <>
              <div className="settings-section">
                <h3>LLM 供应商</h3>
                <div className="provider-selector">
                  {providers.map((p) => (
                    <button
                      key={p.id}
                      className={`provider-btn ${p.id === activeProvider ? "active" : ""}`}
                      onClick={() => setActiveProvider(p.id)}
                    >
                      <span className="provider-name">{p.name}</span>
                      <span className={`provider-status ${p.has_key ? "configured" : "unconfigured"}`}>
                        {p.has_key ? "已配置" : "未配置"}
                      </span>
                    </button>
                  ))}
                  <button className="provider-btn" onClick={addCustomProvider}>＋ 自定义供应商</button>
                </div>
              </div>

              {providerMeta && (
                <div className="settings-section" key={activeProvider}>
                  <h3>{providerMeta.name} 配置</h3>

                  {isCustom && (
                    <div className="form-group">
                      <label>供应商名称</label>
                      <input className="form-input" value={(currentEdit.name as string) || providerMeta.name}
                        onChange={(e) => update(activeProvider, "name", e.target.value)} />
                    </div>
                  )}

                  <div className="form-group">
                    <label>API Key</label>
                    <input
                      type="password"
                      className="form-input"
                      placeholder={currentEdit.has_key ? "已配置，输入新值覆盖" : "输入 API Key"}
                      value={(currentEdit.api_key as string) || ""}
                      onChange={(e) => update(activeProvider, "api_key", e.target.value)}
                    />
                  </div>

                  <div className="form-group">
                    <label>接口地址 (Base URL)</label>
                    <input
                      type="text"
                      className="form-input"
                      value={(currentEdit.base_url as string) || ""}
                      onChange={(e) => update(activeProvider, "base_url", e.target.value)}
                    />
                  </div>

                  <div className="form-group">
                    <label>自定义 Header（每行一个，可留空）</label>
                    <textarea
                      className="form-input"
                      rows={3}
                      placeholder={"X-Title: My App\nx-opencode-session: {conversation_id}"}
                      value={headersText[activeProvider] ?? ""}
                      onChange={(e) =>
                        setHeadersText((prev) => ({ ...prev, [activeProvider]: e.target.value }))
                      }
                      onBlur={(e) => commitHeaders(activeProvider, e.target.value)}
                    />
                    <small>
                      格式 <code>Key: Value</code> 或 <code>Key=Value</code>（按第一个分隔符切分，值可含冒号）；
                      支持任意多个，可覆盖默认 Authorization 头；<code>#</code> 开头的行忽略。
                      <br />
                      值支持模板变量，发送请求时自动替换：
                      <code>{"{session_id}"}</code> <code>{"{conversation_id}"}</code>{" "}
                      <code>{"{workspace}"}</code> <code>{"{model}"}</code> <code>{"{provider}"}</code>；
                      其中 <code>{"{conversation_id}"}</code> 会按（会话 × 供应商）自动生成稳定 ID 并持久化。
                    </small>
                  </div>

                  <div className="form-group">
                    <label>模型</label>
                    <div className="model-input-group">
                      <input
                        type="text"
                        className="form-input"
                        list={`models-${activeProvider}`}
                        placeholder="输入模型名或从列表选择"
                        value={(currentEdit.model as string) || ""}
                        onChange={(e) => update(activeProvider, "model", e.target.value)}
                      />
                      <datalist id={`models-${activeProvider}`}>
                        {availableModels.map((m) => (
                          <option key={m} value={m} />
                        ))}
                      </datalist>
                      <div className="reasoning-popover">
                        <button
                          className={`reasoning-trigger reasoning-effort-${(currentEdit.reasoning_effort as string) || "off"} ${!reasoningSupported ? "reasoning-unsupported" : ""}`}
                          onClick={() => setReasoningOpen(!reasoningOpen)}
                          title={
                            reasoningSupported
                              ? `推理强度: ${reasoningLabel(currentEdit.reasoning_effort)}`
                              : `当前模型可能不支持推理（列表: ${reasoningModels.join(", ") || "未知"}; 可手动尝试）`
                          }
                          type="button"
                        >
                          {reasoningLabel(currentEdit.reasoning_effort)}
                        </button>
                        {reasoningOpen && (
                          <div className="reasoning-menu">
                            <div className="reasoning-track">
                              {[
                                { value: "", label: "关闭", desc: "常规回答" },
                                { value: "low", label: "低", desc: "轻量推理" },
                                { value: "medium", label: "中", desc: "平衡速度与深度" },
                                { value: "high", label: "高", desc: "深度推理" },
                                { value: "max", label: "最大", desc: "极限推理（Token 消耗大）" },
                              ].map((item) => (
                                <button
                                  key={item.value}
                                  className={`reasoning-option ${currentEdit.reasoning_effort === item.value ? "active" : ""}`}
                                  onClick={() => {
                                    update(activeProvider, "reasoning_effort", item.value);
                                    setReasoningOpen(false);
                                  }}
                                  type="button"
                                >
                                  <span className="reasoning-option-label">{item.label}</span>
                                  <span className="reasoning-option-desc">{item.desc}</span>
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="form-group">
                    <label>该供应商的模型列表（每行一个，可添加多个）</label>
                    <textarea
                      className="form-input"
                      rows={Math.min(6, Math.max(2, availableModels.length))}
                      value={availableModels.join("\n")}
                      onChange={(e) => update(
                        activeProvider,
                        "models",
                        e.target.value.split("\n").map((m) => m.trim()).filter(Boolean),
                      )}
                    />
                    <small>当前上方“模型”字段是实际调用的模型，模型列表用于快速切换和保留多个模型。</small>
                  </div>

                  <div className="form-group">
                    <label>温度 (Temperature): {currentEdit.temperature ?? 0.2}</label>
                    <input
                      type="range"
                      min="0"
                      max="2"
                      step="0.1"
                      className="form-range"
                      value={currentEdit.temperature ?? 0.2}
                      onChange={(e) => update(activeProvider, "temperature", parseFloat(e.target.value))}
                    />
                  </div>

                  <div className="form-group">
                    <label>上下文长度 tokens（留空自动：models.dev 同步/内置表）</label>
                    <input
                      type="number"
                      min="1000"
                      step="1000"
                      className="form-input"
                      placeholder="如 1000000（DeepSeek V4）"
                      value={(currentEdit.context_window as number) ?? ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        update(activeProvider, "context_window", v === "" ? null : parseInt(v, 10));
                      }}
                    />
                  </div>

                  <div className="form-actions">
                    <button className="btn-test" onClick={handleTest} disabled={testing}>
                      {testing ? "测试中…" : "🔄 测试连接"}
                    </button>
                    <button className="btn-save-settings" onClick={handleSave} disabled={saving}>
                      {saving ? "保存中…" : "💾 保存配置"}
                    </button>
                    {isCustom && <button className="btn-test" onClick={removeCustomProvider}>删除供应商</button>}
                  </div>

                  {testResult && (
                    <div className={`test-result ${testResult.ok ? "ok" : "error"}`}>
                      {testResult.ok ? "✅ " : "❌ "}{testResult.message}
                    </div>
                  )}
                </div>
              )}
            </>
          ) : activeTab === "mcp" ? (
            <div className="settings-section">
              <div className="mcp-section-head">
                <h3>MCP Server（stdio）</h3>
                <button className="btn-test" onClick={addMcpServer}>＋ 添加</button>
              </div>
              <p className="mcp-hint">
                外部工具服务器通过 stdio 连接，保存后自动重连；工具以
                <code>mcp_&lt;server&gt;_&lt;tool&gt;</code> 注册，调用时会请求审批。
              </p>
              {Object.entries(mcpServers).map(([name, s]) => {
                const st = mcpStatus.find((x) => x.name === name);
                return (
                  <div className="mcp-server-card" key={name}>
                    <div className="mcp-server-row">
                      {renamingMcp?.from === name ? (
                        <>
                          <input
                            className="form-input mcp-name"
                            autoFocus
                            value={renamingMcp.value}
                            onChange={(e) => setRenamingMcp({ from: name, value: e.target.value })}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") renameMcpServer(name, renamingMcp.value);
                              if (e.key === "Escape") setRenamingMcp(null);
                            }}
                            placeholder="新名称（作为工具前缀 mcp_<name>_）"
                          />
                          <button className="btn-test" title="确认改名" onClick={() => renameMcpServer(name, renamingMcp.value)}>✔</button>
                          <button className="btn-test mcp-remove" title="取消" onClick={() => setRenamingMcp(null)}>✕</button>
                        </>
                      ) : (
                        <>
                          <input
                            className="form-input mcp-name"
                            value={name}
                            readOnly
                            title="服务器名（点击 ✏️ 改名，保存后生效；工具前缀 mcp_<name>_ 随之更新）"
                          />
                          <button
                            className="btn-test"
                            title="重命名此服务器"
                            onClick={() => setRenamingMcp({ from: name, value: name })}
                          >
                            ✏️
                          </button>
                        </>
                      )}
                      <label className="mcp-toggle" title="禁用的服务器不连接">
                        <input
                          type="checkbox"
                          checked={s.enabled !== false}
                          onChange={(e) => updateMcp(name, { enabled: e.target.checked })}
                        />
                        启用
                      </label>
                      {st && st.connected && <span className="mcp-badge ok" title={st.tools.join(", ")}>
                        已连接 · {st.tools.length} 工具
                      </span>}
                      {st && !st.connected && st.error && <span className="mcp-badge err" title={st.error}>连接失败</span>}
                      <button className="btn-test mcp-remove" onClick={() => removeMcpServer(name)}>删除</button>
                    </div>
                    <div className="mcp-server-row">
                      <input
                        className="form-input"
                        placeholder="启动命令，如 npx / uvx / python"
                        value={s.command || ""}
                        onChange={(e) => updateMcp(name, { command: e.target.value })}
                      />
                    </div>
                    <div className="mcp-server-row">
                      <input
                        className="form-input"
                        placeholder="参数（空格分隔），如 -y @modelcontextprotocol/server-sqlite ./data.db"
                        value={mcpArgsText[name] ?? (Array.isArray(s.args) ? s.args.join(" ") : "")}
                        onChange={(e) => setMcpArgsText((prev) => ({ ...prev, [name]: e.target.value }))}
                        onBlur={(e) => updateMcp(name, {
                          args: e.target.value.split(/\s+/).filter(Boolean),
                        })}
                      />
                    </div>
                    {st && !st.connected && st.error && <div className="mcp-error-detail">{st.error}</div>}
                  </div>
                );
              })}
              {Object.keys(mcpServers).length === 0 && (
                <div className="mcp-empty">暂未配置 MCP Server</div>
              )}
              <div className="form-actions">
                <button className="btn-save-settings" onClick={saveMcp} disabled={mcpSaving}>
                  {mcpSaving ? "重连中…" : "💾 保存 MCP 配置"}
                </button>
              </div>
              {mcpResult && <div className="test-result ok">{mcpResult}</div>}
            </div>
          ) : activeTab === "skills" ? (
            <div className="settings-section">
              <h3>技能（Skills）</h3>
              <p className="mcp-hint">
                技能是 <code>.agents/skills/&lt;名称&gt;/SKILL.md</code>（frontmatter: name/description/triggers）。
                Agent 通过技能索引自主加载，也可用 <code>/skill &lt;名称&gt;</code> 显式注入当前任务。
                仅 <code>.agents/skills</code> 可写；<code>.claude/.opencode</code> 等第三方目录只读。
                权限规则（glob → allow/deny/ask）：<b>deny</b> 对 Agent 完全隐藏，<b>ask</b> 使用前弹确认。
              </p>

              <div className="mcp-section-head" style={{ marginTop: 6 }}>
                <span>triggers 自动匹配模式</span>
                <button className="btn-test" onClick={saveTriggerMode}>
                  {triggerModeSaved ? "已保存 ✓" : "保存"}
                </button>
              </div>
              <div className="skills-trigger-mode">
                <label className="mcp-toggle" style={{ marginRight: 16 }}>
                  <input
                    type="radio"
                    name="trigger-mode"
                    checked={triggerMode === "substring"}
                    onChange={() => setTriggerMode("substring")}
                  />
                  子串匹配（旧行为）
                </label>
                <label className="mcp-toggle">
                  <input
                    type="radio"
                    name="trigger-mode"
                    checked={triggerMode === "advanced"}
                    onChange={() => setTriggerMode("advanced")}
                  />
                  高级匹配（词边界 + 正则）
                </label>
              </div>
              <p className="mcp-hint">
                子串匹配：<code>review</code> 命中「reviewed」「code review」等任意包含该词的文本。
                高级匹配：普通词按词边界命中（<code>review</code> 只命中独立单词，不命中 <code>reviewed</code>），
                用 <code>/regex/</code> 包裹可写正则，如 <code>/code.?review|审计/</code>。
                开启高级匹配可显著减少无关技能的误注入，避免挤占上下文。
              </p>

              {skillMsg && <div className={`test-result ${skillMsg.ok ? "ok" : "error"}`}>{skillMsg.text}</div>}

              <div className="mcp-section-head">
                <span>已安装 {skills.length} 个技能</span>
                <label className="btn-test" style={{ cursor: "pointer" }}>
                  📤 导入 zip
                  <input type="file" accept=".zip" style={{ display: "none" }}
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) handleZipFile(f); e.target.value = ""; }} />
                </label>
              </div>

              <div className="skills-import-row">
                <input className="form-input" placeholder="本地目录路径或 GitHub URL（https://github.com/owner/repo）"
                  id="skill-import-source" disabled={skillBusy} />
                <button className="btn-test" disabled={skillBusy}
                  onClick={() => {
                    const el = document.getElementById("skill-import-source") as HTMLInputElement | null;
                    const v = el?.value.trim();
                    if (v) handleImport(v);
                  }}>导入</button>
              </div>

              <div className="skills-create-row">
                <input className="form-input" placeholder="新技能名（kebab-case）" value={newSkill.name}
                  onChange={(e) => setNewSkill({ ...newSkill, name: e.target.value })} disabled={skillBusy} />
                <input className="form-input" placeholder="一句话描述" value={newSkill.description}
                  onChange={(e) => setNewSkill({ ...newSkill, description: e.target.value })} disabled={skillBusy} />
                <button className="btn-test" disabled={skillBusy || !newSkill.name.trim()}
                  onClick={() => void skillAction(async () => {
                    const r = await api.createSkill(newSkill.name.trim(), newSkill.description.trim() || "（未填写描述）", "workspace");
                    setNewSkill({ name: "", description: "" });
                    return `已创建 ${r.name}`;
                  })}>新建</button>
              </div>

              <div className="skills-list">
                {skills.length === 0 && <div className="mcp-empty">暂无技能。可导入 zip / 本地目录 / GitHub 仓库，或新建模板。</div>}
                {skills.map((s) => (
                  <div className="skill-item" key={`${s.scope}-${s.name}`}>
                    <div className="skill-item-main">
                      <span className="skill-item-name">{s.name}</span>
                      {s.permission === "deny" && <span className="skill-perm deny" title="权限规则 deny：对 Agent 隐藏">🚫 禁用</span>}
                      {s.permission === "ask" && <span className="skill-perm ask" title="权限规则 ask：使用前需确认">❓ 需确认</span>}
                      <span className={`skill-scope ${s.scope}`}>{s.scope === "user" ? "用户级" : "工作区"}</span>
                      {!s.writable && <span className="skill-readonly" title="第三方目录只读">只读</span>}
                      <span className="skill-item-desc" title={s.description}>{s.description || "（无描述）"}</span>
                    </div>
                    <div className="skill-item-actions">
                      <button className="btn-test" disabled={skillBusy}
                        onClick={() => void skillAction(async () => {
                          const r = await api.readSkill(s.name);
                          setSkillContent({ name: s.name, content: r.content });
                          return `已加载 ${s.name}`;
                        })}>查看</button>
                      {s.writable && (
                        <button className="btn-test" disabled={skillBusy}
                          onClick={() => {
                            if (window.confirm(`确定删除技能 ${s.name}？（${s.scope === "user" ? "用户级" : "工作区"}）`)) {
                              void skillAction(async () => {
                                await api.deleteSkill(s.name, s.scope);
                                if (skillContent?.name === s.name) setSkillContent(null);
                                return `已删除 ${s.name}`;
                              });
                            }
                          }}>删除</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {skillContent && (
                <div className="skill-viewer">
                  <div className="skill-viewer-head">
                    <span>{skillContent.name} / SKILL.md</span>
                    <button className="modal-close" onClick={() => setSkillContent(null)}>✕</button>
                  </div>
                  <pre className="skill-viewer-body">{skillContent.content}</pre>
                </div>
              )}

              <div className="mcp-section-head" style={{ marginTop: 18 }}>
                <span>权限规则（skill_permissions，glob 模式 → 动作）</span>
                <button className="btn-test" onClick={() => void savePermRules()} disabled={!permDirty && permRules.length === 0}>
                  {permDirty ? "保存规则" : "已保存"}
                </button>
              </div>
              <p className="mcp-hint">
                按配置顺序匹配，首个命中的模式生效，未命中默认 allow。示例：<code>internal-*: deny</code>、<code>experimental-*: ask</code>。
              </p>
              <div className="skills-perm-editor">
                {permRules.map((r, i) => (
                  <div className="skills-perm-row" key={i}>
                    <input className="form-input" placeholder="模式，如 internal-*"
                      value={r.pattern}
                      onChange={(e) => {
                        const next = [...permRules];
                        next[i] = { ...r, pattern: e.target.value };
                        setPermRules(next);
                        setPermDirty(true);
                      }} />
                    <select className="form-input" value={r.action}
                      onChange={(e) => {
                        const next = [...permRules];
                        next[i] = { ...r, action: e.target.value as "allow" | "deny" | "ask" };
                        setPermRules(next);
                        setPermDirty(true);
                      }}>
                      <option value="allow">allow（允许）</option>
                      <option value="deny">deny（禁用）</option>
                      <option value="ask">ask（需确认）</option>
                    </select>
                    <button className="modal-close" title="删除规则"
                      onClick={() => { setPermRules(permRules.filter((_, j) => j !== i)); setPermDirty(true); }}>✕</button>
                  </div>
                ))}
                <button className="btn-test" style={{ marginTop: 6 }}
                  onClick={() => { setPermRules([...permRules, { pattern: "", action: "deny" }]); setPermDirty(true); }}>
                  + 添加规则
                </button>
              </div>
            </div>
          ) : activeTab === "general" ? (
            <div className="settings-section">
              <h3>综合设置</h3>
              <div className="mcp-section-head">
                <span>会话缓存清理</span>
              </div>
              <p className="mcp-hint">
                历史会话会记录其关联的项目目录；如果项目目录已被删除或移动，
                这些会话将无法打开继续开发。点击下方按钮可一键删除所有
                <b>关联项目目录不存在</b>的孤儿会话（删除后无法恢复）。
              </p>
              <div className="form-actions">
                <button className="btn-test" onClick={handleCleanup} disabled={cleaning}>
                  {cleaning ? "清理中…" : "🗑️ 清除无关联项目的会话"}
                </button>
              </div>
              {cleanResult && (
                <div className={`test-result ${cleanResult.ok ? "ok" : "error"}`}>
                  {cleanResult.text}
                </div>
              )}

              <div className="mcp-section-head" style={{ marginTop: 18 }}>
                <span>Skills zip 导入大小上限</span>
              </div>
              <p className="mcp-hint">
                通过 zip 导入技能时，限制上传文件的大小（base64 解码后）。
                超过上限的导入请求将被拒绝。
              </p>
              <div className="form-group">
                <label>大小上限（MB）</label>
                <div className="form-row">
                  <input
                    type="number"
                    className="form-input"
                    min={1}
                    max={200}
                    value={maxZipSize}
                    onChange={(e) => setMaxZipSize(parseInt(e.target.value, 10) || 1)}
                    style={{ width: 120 }}
                  />
                  <button className="btn-test" onClick={saveZipSize}>
                    {zipSaved ? "已保存 ✓" : "保存"}
                  </button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
