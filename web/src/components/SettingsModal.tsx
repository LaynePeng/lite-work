import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { BuiltinPluginInfo, CommunityManifest, LLMProviderMeta, LLMProviderSettings, MCPServerConfig, MCPServerStatus, PluginInfo, SkillInfo } from "../types";

// 语义化版本比较（与后端 plugin_loader.semver_compare 口径一致）：
// 返回 >0（a 更新）/ 0 / <0；解析失败回退字符串比较。
// "1.0.0rc0" 视作 1.0.0 的预发布版（1.0.0 > 1.0.0rc0 > 1.0.0-beta）
function verCompare(a: string, b: string): number {
  const parse = (v: string): [number, number, number, string] | null => {
    const m = /^v?(\d+)\.(\d+)\.(\d+)(.+)?$/.exec((v || "").trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3]), m[4] || ""] : null;
  };
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return (a || "").localeCompare(b || "");
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return (pa[i] as number) - (pb[i] as number);
  }
  if (pa[3] === pb[3]) return 0;
  if (!pa[3]) return 1;
  if (!pb[3]) return -1;
  return pa[3] < pb[3] ? -1 : 1;
}

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
  const [activeTab, setActiveTab] = useState<"llm" | "mcp" | "skills" | "general" | "plugins" | "agents">("llm");
  // 综合设置：孤儿会话清理
  const [cleaning, setCleaning] = useState(false);
  const [cleanResult, setCleanResult] = useState<{ ok: boolean; text: string } | null>(null);
  // 综合设置：zip 大小上限（MB）
  const [maxZipSize, setMaxZipSize] = useState<number>(20);
  const [zipSaved, setZipSaved] = useState(false);
  // 综合设置：任务/工具/子 Agent 超时（秒）
  const [toolTimeout, setToolTimeout] = useState<number>(120);
  const [llmTimeout, setLlmTimeout] = useState<number>(300);
  const [subagentTimeout, setSubagentTimeout] = useState<number>(600);
  const [maxSteps, setMaxSteps] = useState<number>(100);
  const [timeoutSaved, setTimeoutSaved] = useState(false);
  // Skills triggers 匹配模式
  const [triggerMode, setTriggerMode] = useState<"substring" | "advanced">("substring");
  const [triggerModeSaved, setTriggerModeSaved] = useState(false);

  // Skills 管理（独立于 LLM/MCP 配置，操作即时生效）
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillContent, setSkillContent] = useState<{ name: string; content: string } | null>(null);
  const [skillBusy, setSkillBusy] = useState(false);
  const [skillMsg, setSkillMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [newSkill, setNewSkill] = useState({ name: "", description: "" });
  const [editSkill, setEditSkill] = useState<{ name: string; scope: string; description: string } | null>(null);
  // 技能权限规则（glob → allow/deny/ask），保存走 /api/config
  const [permRules, setPermRules] = useState<Array<{ pattern: string; action: "allow" | "deny" | "ask" }>>([]);
  const [permDirty, setPermDirty] = useState(false);

  // Plugins 管理
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [pluginBusy, setPluginBusy] = useState(false);
  const [pluginMsg, setPluginMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // 同名插件已存在时覆盖安装（更新）
  const [pluginOverwrite, setPluginOverwrite] = useState(false);
  // 内置插件元信息（只读展示 + 社区版本对比）
  const [builtinPlugins, setBuiltinPlugins] = useState<BuiltinPluginInfo[]>([]);
  const [community, setCommunity] = useState<CommunityManifest | null>(null);
  const [communityBusy, setCommunityBusy] = useState(false);

  // Agents 管理
  const [agents, setAgents] = useState<import("../types").AgentInfo[]>([]);
  const [agentAllTools, setAgentAllTools] = useState<{ name: string; description: string }[]>([]);
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentMsg, setAgentMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [editingAgent, setEditingAgent] = useState<string | null>(null);
  const [agentDrafts, setAgentDrafts] = useState<Record<string, { tools: string[]; useAll: boolean }>>({});
  const [newAgentForm, setNewAgentForm] = useState({ name: "", description: "", prompt: "" });

  const refreshSkills = useCallback(() => {
    api.skills().then((r) => setSkills(r.skills)).catch(() => setSkills([]));
  }, []);

  const refreshPlugins = useCallback(() => {
    Promise.all([
      api.plugins(),
      api.pluginsBuiltin(),
    ]).then(([u, b]) => {
      setPlugins(u.plugins);
      setBuiltinPlugins(b.plugins);
    }).catch(() => { setPlugins([]); setBuiltinPlugins([]); });
  }, []);

  const fetchCommunity = useCallback(async (url?: string) => {
    setCommunityBusy(true);
    try {
      const m = await api.pluginsCommunity(url);
      setCommunity(m);
    } catch (err) {
      setPluginMsg({ ok: false, text: `拉取社区清单失败: ${(err as Error).message}` });
    } finally {
      setCommunityBusy(false);
    }
  }, []);

  // 社区发现过滤：只显示「比当前生效版本新的更新」和「尚未安装的新插件」；
  // 已是最新（或本地版更新）的隐藏，避免与已安装区重复
  const communityUpdates = useMemo(() => {
    const updates: { cp: CommunityManifest["plugins"][number]; currentVer: string; isLocal: boolean }[] = [];
    const fresh: CommunityManifest["plugins"][number][] = [];
    if (community) {
      for (const cp of community.plugins) {
        const local = plugins.find((p) => p.name === cp.name);
        const builtin = builtinPlugins.find((b) => b.name === cp.name);
        const currentVer = local?.version || builtin?.version || "";
        if (!currentVer) {
          fresh.push(cp);
        } else if (verCompare(cp.version, currentVer) > 0) {
          updates.push({ cp, currentVer, isLocal: !!local });
        }
      }
    }
    return { updates, fresh };
  }, [community, plugins, builtinPlugins]);

  // 社区插件安装源 URL（manifest.path → GitHub 子目录）；无源返回 ""
  const communitySrc = useCallback((name: string) => {
    const cp = community?.plugins.find((p) => p.name === name);
    return cp?.path ? `https://github.com/laynepeng/lite-work-plugins/tree/main/${cp.path}` : "";
  }, [community]);

  // 社区技能过滤：只显示「未安装」和「已安装但社区有更新版」的
  // （更新检测依赖 SKILL.md frontmatter 的 version 字段，社区技能补上后自动生效）
  const communitySkillsView = useMemo(() => {
    const updates: { cs: CommunityManifest["skills"][number]; currentVer: string }[] = [];
    const fresh: CommunityManifest["skills"][number][] = [];
    if (community) {
      for (const cs of community.skills) {
        const installed = skills.find((s) => s.name === cs.name);
        if (!installed) {
          fresh.push(cs);
        } else if (cs.version && installed.version && verCompare(cs.version, installed.version) > 0) {
          updates.push({ cs, currentVer: installed.version });
        }
      }
    }
    return { updates, fresh };
  }, [community, skills]);

  // 已安装插件统一视图：内置 + 本地用户版合并。
  // 本地版（~/.lite-work/plugins/）同名覆盖内置版 → 生效版本 = 本地版本；
  // 没有本地版的显示内置版（随主程序发布）。updateTo = 社区可更新到的版本。
  const installedView = useMemo(() => {
    const userByName = new Map(plugins.map((p) => [p.name, p]));
    const updateByName = new Map(communityUpdates.updates.map((u) => [u.cp.name, u.cp.version]));
    const items: Array<{
      name: string;
      description: string;
      tools: string[];
      removedTools?: string[];
      effectiveVer: string;
      builtinVer?: string;
      source?: string;
      isLocal: boolean;
      updateTo?: string;
    }> = builtinPlugins.map((bp) => {
      const u = userByName.get(bp.name);
      return {
        name: bp.name,
        description: u?.description || bp.description,
        tools: u ? u.tools : bp.tools,
        removedTools: u?.removed_tools,
        effectiveVer: u?.version || bp.version,
        builtinVer: bp.version,
        source: u?.source,
        isLocal: !!u,
        updateTo: updateByName.get(bp.name),
      };
    });
    // 纯本地插件（无内置对应）
    for (const p of plugins) {
      if (!builtinPlugins.some((b) => b.name === p.name)) {
        items.push({
          name: p.name,
          description: p.description,
          tools: p.tools,
          removedTools: p.removed_tools,
          effectiveVer: p.version || "—",
          source: p.source,
          isLocal: true,
          updateTo: updateByName.get(p.name),
        });
      }
    }
    return items;
  }, [plugins, builtinPlugins, communityUpdates]);

  const refreshAgents = useCallback(async () => {
    try {
      const r = await api.agentsTools();
      setAgents(Object.values(r.agents));
      setAgentAllTools(r.tools);
      // 初始化草稿（首次进入时）
      setAgentDrafts((prev) => {
        const next = { ...prev };
        for (const a of Object.values(r.agents)) {
          if (!next[a.id]) {
            const t = a.tools;
            next[a.id] = { tools: Array.isArray(t) ? t : r.tools.map((x) => x.name), useAll: !Array.isArray(t) };
          }
        }
        return next;
      });
    } catch (err) {
      setAgentMsg({ ok: false, text: `加载 Agents 失败: ${(err as Error).message}` });
    }
  }, []);

  useEffect(() => {
    refreshSkills();
    refreshPlugins();
    refreshAgents();
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
      if (typeof c.tool_timeout === "number" && c.tool_timeout > 0) setToolTimeout(c.tool_timeout);
      if (typeof c.llm_timeout === "number" && c.llm_timeout > 0) setLlmTimeout(c.llm_timeout);
      if (typeof c.subagent_timeout === "number" && c.subagent_timeout > 0) setSubagentTimeout(c.subagent_timeout);
      if (typeof c.max_steps === "number" && c.max_steps > 0) setMaxSteps(c.max_steps);
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
      const names = r.skills.map((s) => s.name).join(", ");
      const depsLines: string[] = [];
      for (const s of r.skills as (import("../types").SkillInfo & { deps?: import("../types").SkillDepsReport })[]) {
        if (!s.deps) continue;
        const parts: string[] = [];
        if (s.deps.pip) parts.push(s.deps.pip.ok ? "pip ✅" : "pip ❌");
        if (s.deps.npm) parts.push(s.deps.npm.ok ? "npm ✅" : "npm ❌");
        if (s.deps.env) parts.push("env ✅");
        if (parts.length) depsLines.push(`${s.name}: ${parts.join(", ")}`);
      }
      const msg = depsLines.length ? `已导入 ${names}\n依赖: ${depsLines.join("；")}` : `已导入: ${names}`;
      return msg;
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

  // Plugins 管理（Cordis 插件：~/.lite-work/plugins/ 自动发现）
  const pluginAction = useCallback(async (fn: () => Promise<string>) => {
    setPluginBusy(true);
    setPluginMsg(null);
    try {
      const text = await fn();
      // Electron 本地模式：插件变更后自动重启 Core（全新进程状态，页面随之刷新）。
      // 任务运行中 / 非 Electron 环境则跳过重启——缓存失效机制保证
      // 下一个任务同样能加载新插件（litework/app.py _invalidate_plugin_cache）。
      const restart = window.liteWork?.restartCore;
      if (restart) {
        setPluginMsg({ ok: true, text: `${text}；正在重启 Core…` });
        const r = await restart();
        if (!r.ok) {
          setPluginMsg({ ok: true, text: `${text}；Core 未重启（${r.error}），改动将在下个任务生效` });
          refreshPlugins();
          void refreshAgents();
        }
        return;
      }
      setPluginMsg({ ok: true, text });
      refreshPlugins();
      // 插件工具增删会改变 Agents 页的可分配工具清单，同步刷新
      void refreshAgents();
    } catch (e) {
      setPluginMsg({ ok: false, text: (e as Error).message });
    } finally {
      setPluginBusy(false);
    }
  }, [refreshPlugins, refreshAgents]);

  const handlePluginImport = (source: string, overwrite: boolean, version?: string) => {
    void pluginAction(async () => {
      const r = await api.importPlugin({ source, name: undefined, overwrite, version });
      return `已导入: ${r.plugins.map((p) => p.name).join(", ")}`;
    });
  };

  const handlePluginZip = (file: File, overwrite: boolean) => {
    const reader = new FileReader();
    reader.onload = () => {
      const base64 = (reader.result as string).split(",")[1] ?? "";
      void pluginAction(async () => {
        const r = await api.importPlugin({ zip_base64: base64, name: undefined, overwrite });
        return `已导入: ${r.plugins.map((p) => p.name).join(", ")}`;
      });
    };
    reader.readAsDataURL(file);
  };

  // Agents 管理
  const saveAgent = useCallback(async (agentId: string) => {
    setAgentBusy(true);
    setAgentMsg(null);
    try {
      const draft = agentDrafts[agentId];
      if (!draft) return;
      await api.saveAgent({
        id: agentId,
        tools: draft.useAll ? null : draft.tools,
      });
      setAgentMsg({ ok: true, text: `Agent ${agentId} 已保存` });
      setEditingAgent(null);
      void refreshAgents();
    } catch (err) {
      setAgentMsg({ ok: false, text: `保存失败: ${(err as Error).message}` });
    } finally {
      setAgentBusy(false);
    }
  }, [agentDrafts, refreshAgents]);

  const createNewAgent = useCallback(async () => {
    const { name, description, prompt } = newAgentForm;
    if (!name.trim()) { setAgentMsg({ ok: false, text: "请填写 Agent 名称" }); return; }
    setAgentBusy(true);
    setAgentMsg(null);
    try {
      await api.saveAgent({
        id: name.trim().toLowerCase().replace(/\s+/g, "-"),
        description: description.trim() || "自定义 Agent",
        system_prompt: prompt.trim(),
        tools: null,
        mode: "primary",
      });
      setAgentMsg({ ok: true, text: `Agent ${name} 已创建` });
      setNewAgentForm({ name: "", description: "", prompt: "" });
      void refreshAgents();
    } catch (err) {
      setAgentMsg({ ok: false, text: `创建失败: ${(err as Error).message}` });
    } finally {
      setAgentBusy(false);
    }
  }, [newAgentForm, refreshAgents]);

  const deleteAgent = useCallback(async (agentId: string) => {
    if (!window.confirm(`确定删除 Agent「${agentId}」？`)) return;
    setAgentBusy(true);
    setAgentMsg(null);
    try {
      await api.deleteAgent(agentId);
      setAgentMsg({ ok: true, text: `Agent ${agentId} 已删除` });
      void refreshAgents();
    } catch (err) {
      setAgentMsg({ ok: false, text: `删除失败: ${(err as Error).message}` });
    } finally {
      setAgentBusy(false);
    }
  }, [refreshAgents]);
  const [mcpServers, setMcpServers] = useState<Record<string, MCPServerConfig>>({});
  const [mcpArgsText, setMcpArgsText] = useState<Record<string, string>>({});
  const [mcpStatus, setMcpStatus] = useState<MCPServerStatus[]>([]);
  const [mcpSaving, setMcpSaving] = useState(false);
  const [mcpResult, setMcpResult] = useState<string | null>(null);
  const [reasoningOpen, setReasoningOpen] = useState(false);

  // 自定义 Header 的编辑文本（每行 "Key: Value" 或 "Key=Value"），blur 时解析提交
  const [headersText, setHeadersText] = useState<Record<string, string>>({});
  // 模型列表的编辑文本（每行一个）：编辑期间保留原始文本（允许空行/回车换行），
  // blur / 保存时才解析为数组——受控数组会吃掉空行导致无法回车
  const [modelsText, setModelsText] = useState<Record<string, string>>({});

  // 拉取 LLM 配置并重置全部编辑态（初始化与保存成功后共用：
  // 保存后服务端返回最新配置，用它刷新本地 providers/editing，避免"重开设置才生效"）
  const refreshLLM = useCallback(() => {
    Promise.all([api.llmProviders(), api.llmConfig()]).then(([p, c]) => {
      setProviders(p);
      setActiveProvider((cur) => (c.active && p.some((x) => x.id === cur) ? cur : c.active));
      const edit: Record<string, Partial<LLMProviderSettings>> = {};
      const texts: Record<string, string> = {};
      const mtexts: Record<string, string> = {};
      for (const [pid, s] of Object.entries(c.providers)) {
        edit[pid] = { ...s };
        const headers = (s as LLMProviderSettings).custom_headers || {};
        texts[pid] = Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join("\n");
        mtexts[pid] = ((s as LLMProviderSettings).models || []).join("\n");
      }
      setEditing(edit);
      setHeadersText(texts);
      setModelsText(mtexts);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    Promise.all([api.llmProviders(), api.llmConfig(), api.mcpStatus()]).then(([p, c, m]) => {
      setProviders(p);
      setActiveProvider(c.active);
      // 初始化编辑状态
      const edit: Record<string, Partial<LLMProviderSettings>> = {};
      const texts: Record<string, string> = {};
      const mtexts: Record<string, string> = {};
      for (const [pid, s] of Object.entries(c.providers)) {
        edit[pid] = { ...s };
        const headers = (s as LLMProviderSettings).custom_headers || {};
        texts[pid] = Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join("\n");
        mtexts[pid] = (s.models || []).join("\n");
      }
      setEditing(edit);
      setHeadersText(texts);
      setModelsText(mtexts);
      // MCP：用运行状态初始化可编辑配置
      setMcpStatus(m.servers || []);
      const cfg: Record<string, MCPServerConfig> = {};
      for (const s of m.servers || []) {
        cfg[s.name] = { command: s.command, args: s.args, enabled: s.enabled };
      }
      setMcpServers(cfg);
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
  // 模型下拉候选：以编辑中的列表文本为准（未 blur 的草稿也即时生效）
  const availableModels = modelsText[activeProvider] !== undefined
    ? modelsText[activeProvider].split("\n").map((m) => m.trim()).filter(Boolean)
    : ((currentEdit.models as string[] | undefined) ?? providerMeta?.models ?? []);
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
    setHeadersText((prev) => ({ ...prev, [id]: "" }));
    setModelsText((prev) => ({ ...prev, [id]: "" }));
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
        // 模型列表以编辑文本为准（未 blur 的草稿不丢），空行/空白过滤
        if (modelsText[pid] !== undefined) {
          providersPayload[pid].models = modelsText[pid]
            .split("\n").map((m) => m.trim()).filter(Boolean);
        }
      }
      await api.updateLLMConfig(activeProvider, providersPayload);
      setTestResult({ ok: true, message: "配置已保存" });
      onSaved();
      // 用服务端返回的最新配置刷新本地编辑态与供应商列表：
      // 修复"保存后供应商名不更新、需重开设置"的问题
      refreshLLM();
    } catch (err) {
      setTestResult({ ok: false, message: (err as Error).message });
    } finally {
      setSaving(false);
    }
  }, [activeProvider, editing, headersText, modelsText, onSaved, refreshLLM]);

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

  const saveTimeoutConfig = useCallback(async () => {
    setTimeoutSaved(false);
    try {
      await api.updateConfig({
        tool_timeout: toolTimeout,
        llm_timeout: llmTimeout,
        subagent_timeout: subagentTimeout,
        max_steps: maxSteps,
      });
      setTimeoutSaved(true);
      setTimeout(() => setTimeoutSaved(false), 2000);
      onSaved();
    } catch (err) {
      window.alert(`保存失败: ${(err as Error).message}`);
    }
  }, [toolTimeout, llmTimeout, subagentTimeout, maxSteps, onSaved]);

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
              className={`settings-tab ${activeTab === "plugins" ? "active" : ""}`}
              onClick={() => setActiveTab("plugins")}
            >
              Plugins
            </button>
            <button
              className={`settings-tab ${activeTab === "agents" ? "active" : ""}`}
              onClick={() => setActiveTab("agents")}
            >
              Agents
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
                  <h3>{(isCustom ? (currentEdit.name as string) : providerMeta.name) || providerMeta.name} 配置</h3>

                  {isCustom && (
                    <div className="form-group">
                      <label>供应商名称</label>
                      <input
                        className="form-input"
                        placeholder="给这个供应商起个名字（如 智谱 / 本地 Ollama）"
                        value={(currentEdit.name as string) ?? providerMeta.name}
                        onChange={(e) => update(activeProvider, "name", e.target.value)}
                      />
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
                      rows={Math.min(
                        6,
                        Math.max(2, (modelsText[activeProvider] ?? "").split("\n").filter(Boolean).length),
                      )}
                      placeholder={"deepseek-chat\ndeepseek-reasoner"}
                      value={modelsText[activeProvider] ?? ""}
                      onChange={(e) =>
                        setModelsText((prev) => ({ ...prev, [activeProvider]: e.target.value }))
                      }
                      onBlur={(e) => update(
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
                          onClick={() => setEditSkill(
                            editSkill?.name === s.name ? null : { name: s.name, scope: s.scope, description: s.description }
                          )}>
                          {editSkill?.name === s.name ? "取消" : "编辑"}
                        </button>
                      )}
                      {editSkill?.name === s.name && editSkill.scope === s.scope && (
                        <div className="skill-edit-inline" style={{ display: "inline-flex", gap: 4, marginLeft: 4 }}>
                          <input className="form-input" style={{ width: 200 }}
                            value={editSkill.description}
                            onChange={(e) => setEditSkill({ ...editSkill, description: e.target.value })} />
                          <button className="btn-test" disabled={skillBusy || !editSkill.description.trim()}
                            onClick={() => void skillAction(async () => {
                              await api.updateSkill(s.name, editSkill.description.trim(), s.scope);
                              setEditSkill(null);
                              return `已更新 ${s.name} 的描述`;
                            })}>保存</button>
                        </div>
                      )}
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
          ) : activeTab === "plugins" ? (
            <div className="settings-section">
              <h3>插件（Plugins）</h3>
              <p className="mcp-hint">
                工具以插件形式提供：<b>内置</b>随主程序发布；社区安装 / 手动导入的为<b>本地</b>版本，
                同名时覆盖内置版（删除本地版后自动回退）。
              </p>

              {pluginMsg && <div className={`test-result ${pluginMsg.ok ? "ok" : "error"}`}>{pluginMsg.text}</div>}

              {/* -------- 工具栏：手动导入 + 社区更新检查 -------- */}
              <div className="plugin-toolbar">
                <input className="form-input" placeholder="GitHub URL / 本地目录 / .zip 路径"
                  id="plugin-import-source" disabled={pluginBusy} />
                <button className="btn-test" disabled={pluginBusy}
                  onClick={() => {
                    const el = document.getElementById("plugin-import-source") as HTMLInputElement | null;
                    const v = el?.value.trim();
                    if (v) void handlePluginImport(v, pluginOverwrite, undefined);
                  }}>导入</button>
                <label className="btn-test" style={{ cursor: "pointer" }}>
                  📤 zip
                  <input type="file" accept=".zip" style={{ display: "none" }}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void handlePluginZip(f, pluginOverwrite);
                      e.target.value = "";
                    }} />
                </label>
                <label className="plugin-toolbar-toggle" title="导入同名插件时覆盖已安装的本地版">
                  <input type="checkbox" checked={pluginOverwrite}
                    onChange={(e) => setPluginOverwrite(e.target.checked)} />
                  覆盖已存在
                </label>
                <button className="btn-test" disabled={communityBusy} style={{ marginLeft: "auto" }}
                  onClick={() => void fetchCommunity()}>
                  {communityBusy ? "检查中…" : "⟳ 检查社区更新"}
                </button>
              </div>

              {/* -------- 已安装：内置 + 本地合并，显示当前生效版本 -------- */}
              <div className="mcp-section-head">
                <span>已安装（{installedView.length} 个）</span>
                {community && !communityBusy && (
                  <span className="plugin-status">
                    {communityUpdates.updates.length > 0
                      ? `${communityUpdates.updates.length} 个可更新`
                      : "✔ 全部最新"}
                  </span>
                )}
              </div>
              <div className="skills-list">
                {installedView.length === 0 && (
                  <div className="mcp-empty">尚未发现插件（异常状态，请重启应用）</div>
                )}
                {installedView.map((item) => (
                  <div className="skill-item plugin-item" key={item.name}>
                    <div className="plugin-item-body">
                      <div className="plugin-title-row">
                        <span className="skill-item-name">{item.name}</span>
                        <span className={`plugin-tag ${item.isLocal ? "local" : "builtin"}`}>
                          {item.isLocal ? "本地" : "内置"}
                        </span>
                        <span className="plugin-version">v{item.effectiveVer}</span>
                        {item.isLocal && item.builtinVer && (
                          <span className="plugin-tag builtin" title="删除本地版后回退到此版本">
                            内置 v{item.builtinVer}
                          </span>
                        )}
                        {item.updateTo && (
                          <span className="plugin-upd-hint" title="社区有新版本">→ v{item.updateTo} 可更新</span>
                        )}
                      </div>
                      <span className="plugin-desc-row" title={item.description}>{item.description}</span>
                      <div className="plugin-tools">
                        {item.tools.length > 0
                          ? <>{item.tools.slice(0, 8).map((t) => <span className="plugin-tool" key={t}>{t}</span>)}
                              {item.tools.length > 8 && <span className="plugin-tool">+{item.tools.length - 8}</span>}</>
                          : <span className="mcp-empty-inline">（未发现工具）</span>}
                        {item.removedTools && item.removedTools.length > 0 && (
                          <span className="mcp-empty-inline">移除: {item.removedTools.join(", ")}</span>
                        )}
                      </div>
                    </div>
                    <div className="skill-item-actions">
                      {item.updateTo && (
                        <button className="btn-update" disabled={pluginBusy || communityBusy}
                          title={`更新到社区版 v${item.updateTo}`}
                          onClick={() => void handlePluginImport(communitySrc(item.name), true, item.updateTo)}>
                          更新
                        </button>
                      )}
                      {item.isLocal && (
                        <button className="btn-test" disabled={pluginBusy}
                          title={item.builtinVer ? "删除本地版，回退内置版" : "彻底删除该插件"}
                          onClick={() => void pluginAction(async () => {
                            const r = await api.deletePlugin(item.name);
                            return item.builtinVer
                              ? `已删除本地版 ${r.name}，回退内置版 v${item.builtinVer}`
                              : `已彻底删除 ${r.name}`;
                          })}>删除</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {/* -------- 社区发现：只显示可更新 + 未安装的新插件 -------- */}
              {community && (
                <>
                  <div className="mcp-section-head" style={{ marginTop: 12 }}>
                    <span>
                      社区发现（可更新 {communityUpdates.updates.length} · 新插件 {communityUpdates.fresh.length}）
                    </span>
                  </div>
                  {communityUpdates.updates.length + communityUpdates.fresh.length === 0 ? (
                    <div className="mcp-empty-inline" style={{ padding: "6px 2px" }}>
                      ✔ 已安装插件均为最新版本
                    </div>
                  ) : (
                    <div className="skills-list">
                      {communityUpdates.updates.map(({ cp, currentVer, isLocal }) => (
                        <div className="skill-item plugin-item" key={cp.name}>
                          <div className="plugin-item-body">
                            <div className="plugin-title-row">
                              <span className="skill-item-name">{cp.name}</span>
                              <span className={`plugin-tag ${isLocal ? "local" : "builtin"}`}>
                                {isLocal ? "本地" : "内置"}
                              </span>
                              <span className="plugin-ver-diff">
                                v{currentVer} → <b>v{cp.version}</b>
                              </span>
                            </div>
                            <span className="plugin-desc-row" title={cp.description}>{cp.description}</span>
                          </div>
                          <div className="skill-item-actions">
                            <button className="btn-update" disabled={pluginBusy || !communitySrc(cp.name)}
                              onClick={() => void handlePluginImport(communitySrc(cp.name), true, cp.version)}>
                              更新
                            </button>
                          </div>
                        </div>
                      ))}
                      {communityUpdates.fresh.map((cp) => (
                        <div className="skill-item plugin-item" key={cp.name}>
                          <div className="plugin-item-body">
                            <div className="plugin-title-row">
                              <span className="skill-item-name">{cp.name}</span>
                              <span className="plugin-tag new">未安装</span>
                              <span className="plugin-version">v{cp.version}</span>
                            </div>
                            <span className="plugin-desc-row" title={cp.description}>{cp.description}</span>
                          </div>
                          <div className="skill-item-actions">
                            <button className="btn-test" disabled={pluginBusy || !communitySrc(cp.name)}
                              onClick={() => void handlePluginImport(communitySrc(cp.name), true, cp.version)}>
                              安装
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}

              {/* -------- 社区技能：只显示可更新 + 未安装（安装到用户级 ~/.agents/skills/） -------- */}
              {community && community.skills.length > 0 && (
                <>
                  <div className="mcp-section-head" style={{ marginTop: 12 }}>
                    <span>
                      社区技能（可更新 {communitySkillsView.updates.length} · 未安装 {communitySkillsView.fresh.length}，装到 ~/.agents/skills/）
                    </span>
                  </div>
                  {communitySkillsView.updates.length + communitySkillsView.fresh.length === 0 ? (
                    <div className="mcp-empty-inline" style={{ padding: "6px 2px" }}>
                      ✔ 已安装的社区技能均为最新
                    </div>
                  ) : (
                    <div className="skills-list">
                      {communitySkillsView.updates.map(({ cs, currentVer }) => {
                        const srcUrl = cs.path
                          ? `https://github.com/laynepeng/lite-work-plugins/tree/main/${cs.path}`
                          : "";
                        return (
                          <div className="skill-item plugin-item" key={cs.name}>
                            <div className="plugin-item-body">
                              <div className="plugin-title-row">
                                <span className="skill-item-name">{cs.name}</span>
                                <span className="plugin-tag local">本地</span>
                                <span className="plugin-ver-diff">
                                  v{currentVer} → <b>v{cs.version}</b>
                                </span>
                              </div>
                              <span className="plugin-desc-row" title={cs.description}>{cs.description}</span>
                            </div>
                            <div className="skill-item-actions">
                              <button className="btn-update" disabled={pluginBusy || !srcUrl}
                                title="覆盖更新（保留本地 .env 配置）"
                                onClick={() => void pluginAction(async () => {
                                  const r = await api.importSkill({ source: srcUrl, scope: "user", overwrite: true });
                                  refreshSkills();
                                  return `已更新技能: ${r.skills.map((s) => s.name).join(", ")}`;
                                })}>更新</button>
                            </div>
                          </div>
                        );
                      })}
                      {communitySkillsView.fresh.map((cs) => {
                        const srcUrl = cs.path
                          ? `https://github.com/laynepeng/lite-work-plugins/tree/main/${cs.path}`
                          : "";
                        return (
                          <div className="skill-item plugin-item" key={cs.name}>
                            <div className="plugin-item-body">
                              <div className="plugin-title-row">
                                <span className="skill-item-name">{cs.name}</span>
                                <span className="plugin-tag new">未安装</span>
                                <span className="plugin-version">v{cs.version}</span>
                              </div>
                              <span className="plugin-desc-row" title={cs.description}>{cs.description}</span>
                            </div>
                            <div className="skill-item-actions">
                              <button className="btn-test" disabled={pluginBusy || !srcUrl}
                                onClick={() => void pluginAction(async () => {
                                  const r = await api.importSkill({ source: srcUrl, scope: "user" });
                                  refreshSkills();
                                  return `已安装技能: ${r.skills.map((s) => s.name).join(", ")}`;
                                })}>安装</button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}
            </div>
          ) : activeTab === "agents" ? (
            <div className="settings-section">
              <h3>Agents（工作模式）</h3>
              <p className="mcp-hint">
                每个 Agent 是一套独立的工作人格与工具集。内置 build/plan/office/research 可调整工具白名单；
                可新建自定义 Agent（描述 + 系统提示词 + 工具集）。工具覆盖内置与所有已安装插件。
              </p>
              {agentMsg && <div className={`test-result ${agentMsg.ok ? "ok" : "error"}`}>{agentMsg.text}</div>}

              <div className="mcp-section-head">
                <span>全部 Agent（{agents.length}）</span>
                <button className="btn-test" onClick={() => void refreshAgents()}>刷新</button>
              </div>

              <div className="skills-list">
                {agents.map((a) => {
                  const draft = agentDrafts[a.id] || { tools: [], useAll: true };
                  const isBuiltin = ["build", "plan", "office", "research"].includes(a.id);
                  const isEditing = editingAgent === a.id;
                  return (
                    <div className="skill-item" key={a.id} style={{ flexDirection: "column", alignItems: "stretch" }}>
                      <div className="skill-item-main" style={{ width: "100%" }}>
                        <span className="skill-item-name">{a.id}</span>
                        {isBuiltin
                          ? <span className="skill-scope user">内置</span>
                          : <span className="skill-scope workspace">自定义</span>}
                        <span className="skill-item-desc">{a.description || "（无描述）"}</span>
                        <div className="skill-item-actions">
                          <button className="btn-test" disabled={agentBusy}
                            onClick={() => setEditingAgent(isEditing ? null : a.id)}>
                            {isEditing ? "收起" : "编辑工具"}
                          </button>
                          {!isBuiltin && (
                            <button className="btn-test" disabled={agentBusy}
                              onClick={() => void deleteAgent(a.id)}>删除</button>
                          )}
                        </div>
                      </div>
                      {isEditing && (
                        <div className="agent-tool-editor">
                          <label className="mcp-toggle" style={{ marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}>
                            <input type="checkbox" checked={draft.useAll}
                              onChange={(e) => setAgentDrafts((p) => ({
                                ...p, [a.id]: { ...p[a.id], useAll: e.target.checked,
                                  tools: p[a.id]?.tools || agentAllTools.map((t) => t.name) },
                              }))} />
                            使用全部工具（含未来新增插件）
                          </label>
                          {!draft.useAll && (
                            <div className="agent-tool-grid">
                              {agentAllTools.map((t) => {
                                const checked = draft.tools.includes(t.name);
                                return (
                                  <label key={t.name} className="agent-tool-check"
                                    title={t.description}>
                                    <input type="checkbox" checked={checked}
                                      onChange={(e) => {
                                        const next = new Set(draft.tools);
                                        if (e.target.checked) next.add(t.name); else next.delete(t.name);
                                        setAgentDrafts((p) => ({ ...p, [a.id]: { ...p[a.id], tools: [...next] } }));
                                      }} />
                                    {t.name}
                                  </label>
                                );
                              })}
                            </div>
                          )}
                          <div className="form-actions" style={{ marginTop: 8 }}>
                            <button className="btn-test" disabled={agentBusy}
                              onClick={() => void saveAgent(a.id)}>保存工具</button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="mcp-section-head" style={{ marginTop: 14 }}>
                <span>新建自定义 Agent</span>
              </div>
              <div className="skills-create-row" style={{ flexWrap: "wrap" }}>
                <input className="form-input" style={{ flex: "1 1 140px" }} placeholder="名称（英文，如 support）"
                  value={newAgentForm.name}
                  onChange={(e) => setNewAgentForm({ ...newAgentForm, name: e.target.value })} />
                <input className="form-input" style={{ flex: "2 1 220px" }} placeholder="一句话描述"
                  value={newAgentForm.description}
                  onChange={(e) => setNewAgentForm({ ...newAgentForm, description: e.target.value })} />
              </div>
              <textarea className="form-input" style={{ marginTop: 6 }} rows={3}
                placeholder="系统提示词（定义该 Agent 的人格与工作准则）"
                value={newAgentForm.prompt}
                onChange={(e) => setNewAgentForm({ ...newAgentForm, prompt: e.target.value })} />
              <div className="form-actions" style={{ marginTop: 8 }}>
                <button className="btn-test" disabled={agentBusy || !newAgentForm.name.trim()}
                  onClick={() => void createNewAgent()}>创建 Agent</button>
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

              <div className="mcp-section-head" style={{ marginTop: 18 }}>
                <span>执行超时与步数（秒）</span>
                <button className="btn-test" onClick={() => void saveTimeoutConfig()}>
                  {timeoutSaved ? "已保存 ✓" : "保存"}
                </button>
              </div>
              <p className="mcp-hint">
                工具调用超时：单个工具最长执行时间；LLM 请求超时：单次模型调用最长等待；
                子 Agent 超时：派生子任务整体最长执行时间；最大步数：单个任务最多工具调用轮数。
              </p>
              <div className="timeout-grid">
                <div className="form-group">
                  <label>工具调用超时（秒）</label>
                  <input
                    type="number" className="form-input" min={5} max={3600}
                    value={toolTimeout}
                    onChange={(e) => setToolTimeout(Math.max(1, parseInt(e.target.value, 10) || 120))}
                  />
                </div>
                <div className="form-group">
                  <label>LLM 请求超时（秒）</label>
                  <input
                    type="number" className="form-input" min={5} max={3600}
                    value={llmTimeout}
                    onChange={(e) => setLlmTimeout(Math.max(1, parseInt(e.target.value, 10) || 300))}
                  />
                </div>
                <div className="form-group">
                  <label>子 Agent 超时（秒）</label>
                  <input
                    type="number" className="form-input" min={5} max={3600}
                    value={subagentTimeout}
                    onChange={(e) => setSubagentTimeout(Math.max(1, parseInt(e.target.value, 10) || 600))}
                  />
                </div>
                <div className="form-group">
                  <label>最大步数</label>
                  <input
                    type="number" className="form-input" min={1} max={500}
                    value={maxSteps}
                    onChange={(e) => setMaxSteps(Math.max(1, parseInt(e.target.value, 10) || 100))}
                  />
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
