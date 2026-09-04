import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { AgentInfo, CommandInfo, LLMConfig, LLMProviderMeta, SessionModel, SkillInfo } from "../types";

export default function Composer({
  disabled = false,
  running,
  agents,
  currentAgent,
  onSelectAgent,
  onSend,
  onStop,
  llmConfig,
  providerMeta,
  sessionModel,
  onSessionModelChange,
  reasoningEffort = "",
  onReasoningEffortChange,
}: {
  disabled?: boolean;
  running: boolean;
  agents: AgentInfo[];
  currentAgent: string;
  onSelectAgent: (id: string) => void;
  onSend: (prompt: string) => void;
  onStop: () => void;
  kind?: never;
  llmConfig: LLMConfig | null;
  providerMeta?: LLMProviderMeta[];
  sessionModel: SessionModel | null;
  onSessionModelChange: (model: SessionModel | null) => void;
  reasoningEffort?: string;
  onReasoningEffortChange?: (v: string) => void;
}) {
  const [text, setText] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [selIdx, setSelIdx] = useState(0);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  // 上传反馈提示（"上传中… / N 个文件已上传"）：操作反馈类，几秒后自动消失
  const [uploadToast, setUploadToast] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = (msg: string) => {
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
    setUploadToast(msg);
    toastTimer.current = window.setTimeout(() => setUploadToast(null), 2500);
  };

  // 组件卸载时清理提示定时器
  useEffect(() => () => {
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current);
  }, []);

  // Agent 图标与中文名（GAI 通用入口：办公/调研/代码一站式）
  const AGENT_META: Record<string, { icon: string; label: string }> = {
    build: { icon: "💻", label: "代码" },
    plan: { icon: "📋", label: "规划" },
    office: { icon: "📄", label: "办公" },
    research: { icon: "🔎", label: "调研" },
  };
  const agentMeta = (id: string) => AGENT_META[id] ?? { icon: "🤖", label: id };

  // ------------------------------------------------------------ 输入历史

  // 发送过的输入历史（localStorage 持久化，最近 100 条）：
  // 输入框为空时按 ↑/↓ 翻阅；/history 命令查看列表
  const HISTORY_KEY = "litework.inputHistory";
  const HISTORY_MAX = 100;

  const [showHistory, setShowHistory] = useState(false);
  const [historyIdx, setHistoryIdx] = useState(-1); // -1 = 不在翻阅态
  const historyRef = useRef<string[]>([]);

  const loadHistory = useCallback((): string[] => {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter((x) => typeof x === "string") : [];
    } catch {
      return [];
    }
  }, []);

  const pushHistory = useCallback((input: string) => {
    const t = input.trim();
    if (!t) return;
    const list = loadHistory();
    // 与最近一条相同不重复记录
    if (list[0] === t) return;
    list.unshift(t);
    if (list.length > HISTORY_MAX) list.length = HISTORY_MAX;
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
    } catch { /* 存储满等情况忽略 */ }
    historyRef.current = list;
  }, [loadHistory]);

  // 挂载时预热缓存（/history 面板与翻阅共用）
  useEffect(() => {
    historyRef.current = loadHistory();
  }, [loadHistory]);

  /** 输入框为空时按 ↑：翻上一条；↓：往回翻；到底恢复空。 */
  const navigateHistory = (dir: "up" | "down"): boolean => {
    const list = historyRef.current;
    if (list.length === 0) return false;
    let idx = historyIdx;
    if (dir === "up") {
      idx = idx < 0 ? 0 : Math.min(idx + 1, list.length - 1);
    } else {
      if (idx < 0) return false; // 不在翻阅态，↓ 无操作
      idx = idx - 1;
      if (idx < 0) {
        // 翻到底（最新之前）→ 恢复空输入，退出翻阅态
        setHistoryIdx(-1);
        setText("");
        return true;
      }
    }
    setHistoryIdx(idx);
    setText(list[idx]);
    return true;
  };

  /** 退出历史翻阅态（用户手动编辑时）。 */
  const exitHistoryMode = () => {
    if (historyIdx >= 0) setHistoryIdx(-1);
  };

  /** /history 面板：点击回填输入框。 */
  const pickHistory = (item: string) => {
    setText(item);
    setShowHistory(false);
    setHistoryIdx(-1);
    inputRef.current?.focus();
  };

  // 上传文件（📎 按钮 / 粘贴图片 / 拖拽文件共用）：
  // 成功后把工作区相对路径以引用形式插入输入框，随消息发给 Agent
  const uploadFiles = async (files: File[]) => {
    if (disabled || files.length === 0) return;
    setUploading(true);
    showToast("上传中…");
    let ok = 0;
    try {
      for (const file of files) {
        const isImage = file.type.startsWith("image/");
        try {
          const resp = await api.uploadFile(file);
          setText((t) => (t ? `${t}\n` : "") + `${isImage ? "🖼" : "📎"} 已上传${isImage ? "图片" : "文件"}：${resp.path}（请读取并处理该${isImage ? "图片" : "文件"}）`);
          ok++;
        } catch (err) {
          // 失败不静默：行内错误写进输入框，用户可见可删
          setText((t) => (t ? `${t}\n` : "") + `⚠ 文件上传失败：${file.name}（${err instanceof Error ? err.message : String(err)}）`);
        }
      }
      showToast(ok > 0 ? `已上传 ${ok} 个文件` : "上传失败，详情见输入框");
      inputRef.current?.focus();
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // 📎 按钮选文件
  const handleFilesPicked = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    void uploadFiles(Array.from(files));
  };

  // 粘贴图片：仅当剪贴板里确有图片文件时才接管，纯文本粘贴完全走默认行为
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (disabled) return;
    const images = Array.from(e.clipboardData?.items ?? [])
      .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null);
    if (images.length === 0) return;
    e.preventDefault();
    void uploadFiles(images);
  };

  // 拖拽文件：dataTransfer 里有文件才接管（阻止默认跳转），拖文本不受影响
  const handleDragOver = (e: React.DragEvent<HTMLTextAreaElement>) => {
    if (disabled) return;
    if (Array.from(e.dataTransfer.types).includes("Files")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      setDragOver(true);
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLTextAreaElement>) => {
    setDragOver(false);
    if (disabled) return;
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length === 0) return;
    e.preventDefault();
    void uploadFiles(files);
  };

  // 推理强度中文标签
  const reasoningLabel = (v: string) => {
    switch (v) {
      case "low": return "低";
      case "medium": return "中";
      case "high": return "高";
      case "max": return "最大";
      default: return "关";
    }
  };

  // ------------------------------------------------------------ 生效值计算
  // 模型下拉显示「当前对话实际生效的模型/effort」：
  // 会话级 override 优先，否则回退到全局 provider 配置（与后端 create_loop 语义一致）
  const allProviders = providerMeta && providerMeta.length > 0
    ? providerMeta
    : (llmConfig ? Object.entries(llmConfig.providers).map(([id, provider]) => ({
        id, name: provider.name || id, models: provider.models, has_key: provider.has_key,
        kind: "openai" as const, default_base_url: "", model: provider.model,
      })) : []);
  const effProviderId = sessionModel?.provider ?? llmConfig?.active ?? "";
  const effProviderMeta = allProviders.find((p) => p.id === effProviderId);
  const effProviderName = effProviderMeta?.name || effProviderId;
  const effProviderCfg = llmConfig?.providers?.[effProviderId];
  const globalModel = effProviderCfg?.model ?? "";
  // provider 级默认 effort（设置弹窗配的，config.json providers[pid].reasoning_effort）
  const providerEffort = effProviderCfg?.reasoning_effort ?? "";
  // 会话级 override：""=跟随 provider 默认，"off"=显式关闭，档位=具体值
  const hasEffortOverride = reasoningEffort !== undefined && reasoningEffort !== "";
  const displayEffort = hasEffortOverride ? reasoningEffort : (providerEffort || "off");
  // 生效模型：override 或全局 provider 默认
  const effModel = sessionModel?.model ?? globalModel;
  const effSelectValue = `${effProviderId}\n${effModel}`;
  const globalSelectValue = `${llmConfig?.active ?? ""}\n${globalModel}`;
  const isDefaultModel = !sessionModel;
  // 会话 override 的 (provider, model) 是否已被下方「已配置 Key 的供应商模型列表」覆盖：
  // 已覆盖 → 不需要兜底项（否则与正常 option value 重复且排在前面，浏览器会选中兜底项，
  //          显示内部 ID 如 custom_xxx 而不是供应商显示名）；
  // 未覆盖（供应商被删 / Key 被清除 / 模型列表变更）→ 补一项避免 <select> 空白。
  const hasProviderOption = (pid: string, mid: string) =>
    allProviders.some((p) => p.id === pid && p.has_key && (p.models ?? []).includes(mid));
  const showFallbackOption = !isDefaultModel && !!sessionModel &&
    !(sessionModel.model === globalModel && sessionModel.provider === llmConfig?.active) &&
    !hasProviderOption(sessionModel.provider, sessionModel.model);
  // 兜底项文案优先使用供应商显示名（自定义供应商配置的 name），查不到才退化为内部 ID
  const fallbackProviderName =
    allProviders.find((p) => p.id === sessionModel?.provider)?.name || sessionModel?.provider || "";

  // 打开面板时懒加载命令与技能列表
  useEffect(() => {
    if (!paletteOpen) return;
    let cancelled = false;
    void (async () => {
      try {
        const [cmdResp, skillResp] = await Promise.all([
          api.commands().catch(() => ({ commands: [] })),
          api.skills().catch(() => ({ skills: [] })),
        ]);
        if (!cancelled) {
          setCommands(cmdResp.commands);
          setSkills(skillResp.skills);
        }
      } catch {
        /* 面板数据加载失败不阻断输入 */
      }
    })();
    return () => { cancelled = true; };
  }, [paletteOpen]);

  // 面板打开时解析当前输入
  const input = text;
  const startsWithSlash = input.startsWith("/");
  const panelVisible = paletteOpen && startsWithSlash && !running;
  const tokens = startsWithSlash ? input.slice(1).split(/\s+/) : [];
  const cmdToken = tokens[0] ?? "";
  const restAfterCmd = input.slice(1 + cmdToken.length).replace(/^\s+/, "");
  const pickingSkill = cmdToken.toLowerCase() === "skill" && !restAfterCmd.includes(" ");

  const filtered = useMemo(() => {
    const q = cmdToken.toLowerCase();
    return commands.filter((c) => c.name.toLowerCase().startsWith(q));
  }, [commands, cmdToken]);

  const filteredSkills = useMemo(() => {
    if (!pickingSkill) return [];
    const q = restAfterCmd.toLowerCase();
    return skills.filter((s) => s.name.toLowerCase().includes(q));
  }, [skills, pickingSkill, restAfterCmd]);

  const candidates: { name: string; description: string; hint: string }[] = pickingSkill
    ? filteredSkills.map((s) => ({ name: s.name, description: s.description || "技能", hint: "skill" }))
    : filtered.map((c) => ({ name: c.name, description: c.description, hint: c.argsHint }));

  // 输入变化时重置选中索引
  useEffect(() => { setSelIdx(0); }, [input]);

  const applySuggestion = (name: string) => {
    if (pickingSkill) {
      setText(`/skill ${name} `);
    } else {
      setText(`/${name} `);
    }
    inputRef.current?.focus();
  };

  const submit = () => {
    const t = text.trim();
    // 任务运行中也允许提交：作为补充指令排队，下一回合注入对话
    if (!t || disabled) return;
    setPaletteOpen(false);
    // /history：本地命令（不消耗 LLM），弹出历史输入列表
    if (t === "/history" || t === "/history ") {
      historyRef.current = loadHistory();
      setShowHistory(true);
      setText("");
      return;
    }
    pushHistory(t);
    setHistoryIdx(-1);
    onSend(t);
    setText("");
  };

  const primary = agents.filter((a) => a.mode !== "subagent");

  return (
    <div className="composer-wrap">
      {primary.length > 0 && (
        <div className="agent-bar" role="group" aria-label="选择 Agent">
          <span className="agent-bar-label">Agent:</span>
          {primary.map((a) => {
            const meta = agentMeta(a.id);
            return (
              <button
                key={a.id}
                className={`agent-btn ${currentAgent === a.id ? "active" : ""}`}
                title={a.description}
                onClick={() => onSelectAgent(a.id)}
                disabled={disabled || running}
              >
                <span className="agent-btn-icon" aria-hidden>{meta.icon}</span>
                <span className="agent-btn-label">{meta.label}</span>
              </button>
            );
          })}
          <span className="agent-bar-hint" title="按 Tab 在 Agent 之间切换">
            Tab
          </span>
          <span className="agent-bar-label">模型:</span>
          <select
            className="model-select"
            value={isDefaultModel ? "__global__" : effSelectValue}
            onChange={(e) => {
              const v = e.target.value;
              if (!v) return;
              if (v === globalSelectValue || v === "__global__") {
                // 选回全局默认 → 清除会话 override
                onSessionModelChange(null);
                return;
              }
              const [provider, model] = v.split("\n");
              onSessionModelChange({ provider, model });
            }}
            disabled={disabled || running}
            title={isDefaultModel ? `当前使用全局默认模型（${effProviderName} / ${effModel}），选择可切换为会话专用` : `当前会话模型：${effProviderName} / ${effModel}；选中“全局默认”可恢复`}
          >
            {/* 始终提供「全局默认」入口：显示实际生效的默认模型名，不再是抽象占位 */}
            <option value="__global__">
              {llmConfig?.active ? `${allProviders.find((p) => p.id === llmConfig.active)?.name || llmConfig.active} / ${llmConfig.providers[llmConfig.active]?.model || "未配置"}` : "未配置全局模型"}
              {isDefaultModel ? "" : "（默认）"}
            </option>
            {showFallbackOption && sessionModel && (
              // 会话 override 确实不在任何已配置的 provider 模型列表里（供应商被删/模型列表变更）
              // 才补这一项避免 select 空白；文案用供应商显示名而非内部 ID（如 custom_xxx）
              <option value={`${sessionModel.provider}\n${sessionModel.model}`}>
                {fallbackProviderName} / {sessionModel.model}（会话）
              </option>
            )}
            {allProviders.map((provider) =>
              provider.has_key && (provider.models ?? []).map((model) => (
                <option key={`${provider.id}:${model}`} value={`${provider.id}\n${model}`}>
                  {provider.name || provider.id} / {model}
                  {model === globalModel && provider.id === llmConfig?.active ? "（默认）" : ""}
                </option>
              ))
            )}
          </select>
          {onReasoningEffortChange && (
            <div className="reasoning-popover reasoning-quick">
              <button
                type="button"
                className={`reasoning-trigger reasoning-quick-trigger reasoning-effort-${displayEffort === "off" ? "off" : displayEffort}`}
                onClick={() => setReasoningOpen(!reasoningOpen)}
                title={
                  hasEffortOverride
                    ? `推理强度：${reasoningLabel(reasoningEffort)}（会话级）`
                    : providerEffort
                      ? `推理强度：${reasoningLabel(providerEffort)}（供应商默认，${effProviderName} 设置中配置）`
                      : "推理强度：关闭（未配置）"
                }
              >
                {reasoningLabel(displayEffort)}
              </button>
              {reasoningOpen && (
                <div className="reasoning-menu">
                  <div className="reasoning-track">
                    {providerEffort && (
                      <button
                        className={`reasoning-option ${!hasEffortOverride ? "active" : ""}`}
                        onClick={() => { onReasoningEffortChange(""); setReasoningOpen(false); }}
                        type="button"
                      >
                        <span className="reasoning-option-label">跟随全局 · {reasoningLabel(providerEffort)}</span>
                        <span className="reasoning-option-desc">使用供应商默认（{effProviderName} 设置中配置）</span>
                      </button>
                    )}
                    {[
                      { value: "off", label: "关闭", desc: "常规回答（本会话）" },
                      { value: "low", label: "低", desc: "轻量推理" },
                      { value: "medium", label: "中", desc: "平衡速度与深度" },
                      { value: "high", label: "高", desc: "深度推理" },
                      { value: "max", label: "最大", desc: "极限推理（Token 消耗大）" },
                    ].map((item) => (
                      <button
                        key={item.value}
                        className={`reasoning-option ${hasEffortOverride && reasoningEffort === item.value ? "active" : ""}`}
                        onClick={() => { onReasoningEffortChange(item.value); setReasoningOpen(false); }}
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
          )}
        </div>
      )}
      <div className={`composer ${dragOver ? "drag-over" : ""}`}>
        {panelVisible && candidates.length > 0 && (
          <div className="command-palette" role="listbox">
            {candidates.slice(0, 8).map((c, i) => (
              <button
                key={c.name}
                className={`command-palette-item ${i === selIdx ? "active" : ""}`}
                onMouseDown={(e) => { e.preventDefault(); applySuggestion(c.name); }}
                onMouseEnter={() => setSelIdx(i)}
              >
                <span className="command-palette-name">/{c.name}</span>
                <span className="command-palette-desc">{c.description}</span>
                {c.hint && <span className="command-palette-hint">{c.hint}</span>}
              </button>
            ))}
          </div>
        )}
        {/* /history：历史输入面板（本地命令，不消耗 LLM） */}
        {showHistory && (
          <div className="history-panel" role="listbox">
            <div className="history-header">
              <span className="history-title">🕘 历史输入（{historyRef.current.length}）</span>
              <span className="history-hint">点击回填 · Esc 关闭</span>
            </div>
            {historyRef.current.length === 0 ? (
              <div className="history-empty">还没有历史输入</div>
            ) : (
              <div className="history-list">
                {historyRef.current.slice(0, 30).map((item, i) => (
                  <button
                    key={`${i}-${item.slice(0, 20)}`}
                    className="history-item"
                    title={item}
                    onClick={() => pickHistory(item)}
                  >
                    <span className="history-item-time">{historyRef.current.length - i}</span>
                    <span className="history-item-text">{item.length > 80 ? item.slice(0, 80) + "…" : item}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <textarea
          autoFocus
          ref={inputRef}
          value={text}
          onChange={(e) => {
            const v = e.target.value;
            setText(v);
            // 用户手动编辑 → 退出历史翻阅态（下次 ↑ 从最新开始）
            exitHistoryMode();
            // 仅首字符输入 "/" 时触发面板（消息中间的斜杠不触发）
            if (v.startsWith("/") && !paletteOpen) setPaletteOpen(true);
            if (!v.startsWith("/")) setPaletteOpen(false);
          }}
          onKeyDown={(e) => {
            if (panelVisible && candidates.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSelIdx((i) => (i + 1) % candidates.length);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setSelIdx((i) => (i - 1 + candidates.length) % candidates.length);
                return;
              }
              if (e.key === "Tab") {
                e.preventDefault();
                applySuggestion(candidates[Math.min(selIdx, candidates.length - 1)].name);
                return;
              }
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                // 有候选且尚未带参数时，Enter 选中补全；已有参数则直接发送
                const needsArgs = pickingSkill ? false : (filtered[selIdx]?.argsHint ?? "") !== "";
                if (needsArgs && !restAfterCmd) {
                  applySuggestion(candidates[Math.min(selIdx, candidates.length - 1)].name);
                } else if (pickingSkill && filteredSkills[selIdx]) {
                  applySuggestion(filteredSkills[selIdx].name);
                } else {
                  submit();
                }
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                setPaletteOpen(false);
                return;
              }
            }
            // /history 面板：Esc 关闭
            if (e.key === "Escape" && showHistory) {
              e.preventDefault();
              setShowHistory(false);
              return;
            }
            // 输入历史翻阅：命令面板未打开时，空输入 ↑ 翻上一条、↓ 往回
            if (!panelVisible && !showHistory && !e.ctrlKey && !e.metaKey && !e.altKey) {
              if (e.key === "ArrowUp" && !text) {
                if (navigateHistory("up")) {
                  e.preventDefault();
                  return;
                }
              }
              if (e.key === "ArrowDown") {
                if (navigateHistory("down")) {
                  e.preventDefault();
                  return;
                }
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          onBlur={() => setTimeout(() => setPaletteOpen(false), 150)}
          onPaste={handlePaste}
          onDragOver={handleDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          placeholder={running ? "任务进行中：输入将加入待发送队列，任务完成后自动发送" : `给 lite-work 下达任务…（输入 / 唤起命令面板）`}
          rows={3}
          disabled={disabled}
        />
        {running ? (
          <>
            <button className="btn-send" onClick={submit} disabled={disabled || !text.trim()} title="加入待发送队列">
              ➤
            </button>
            <button className="btn-send btn-stop" onClick={onStop} title="停止任务">
              <span className="stop-icon" />
            </button>
          </>
        ) : (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={(e) => void handleFilesPicked(e.target.files)}
            />
            <button
              className="btn-upload"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled || uploading}
              title={uploading ? "上传中…" : "上传文件（CSV/Excel/文档等素材，交给 Agent 处理）"}
            >
              {uploading ? "…" : "📎"}
            </button>
            <button className="btn-send" onClick={submit} disabled={disabled || !text.trim()}>
              ➤
            </button>
          </>
        )}
      </div>
      <div className="composer-hint">
        {uploadToast && <span className="upload-toast">{uploadToast}</span>}
        {uploadToast ? " · " : ""}
        {running ? "任务进行中：➤ 追加到待发送队列（任务完成后自动发送），■ 停止任务" : "工具执行受安全策略保护，中危操作会请求你确认"}
      </div>
    </div>
  );
}
