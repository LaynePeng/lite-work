// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 盯梢 / 定时计划管理（右栏「盯梢」页签内）。
//
// 设计取舍：
//   - 列表 + 内联编辑器（不做二级弹窗）：计划字段不多，就地展开改比跳页快；
//   - 「数据源」决定是否显示探针配置：`source` 为空 = 纯定时任务（到点跑 prompt），
//     非空 = 盯梢（先探测、指纹变了才唤起 Agent）。这是两种心智模型，UI 要显式区分；
//   - 权限档只给 read / read+web 两个选项：无人值守没人点审批，写/执行类
//     必须从工具面移除（后端也会拒），UI 不给误导性的更多选项。

import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type ProbeFieldSpec, type ScheduleInfo, type SchedulePayload, type WatchSourceInfo } from "../api";

interface Props {
  /** 当前会话 id：新建计划默认绑到它（结果写回这个会话） */
  sessionId?: string | null;
  /** 数据源由父组件传入（已加载好的），避免重复请求 */
  sources: WatchSourceInfo[];
  /** 通知列表刷新（计划被改动后） */
  onChanged?: () => void;
}

/** 编辑器里持有的一格探针参数：可能是原始结构，也可能是正在编辑的 JSON 文本 */
type ProbeValue = string | number | boolean | Record<string, unknown> | unknown[];

/**
 * 打开计划时该字段原本是什么类型。
 *
 * 探针参数是任意 JSON，而 input/textarea 里只有文本；不记住原类型，
 * 数字/布尔会在「文本往返」里退化成字符串（插件读 `cfg["threshold"]` 拿到
 * "60" 而不是 60，同样是静默失效），数组/对象则连控件都选不对。
 */
type ProbeKind = "json" | "number" | "boolean";

/** 把一个字段说明归一成结构化元信息（兼容旧的纯字符串写法）。 */
function fieldMeta(spec: ProbeFieldSpec | undefined): Exclude<ProbeFieldSpec, string> {
  if (typeof spec === "string") return { hint: spec };
  return spec ?? {};
}

/** 值是否为需要 JSON 编辑器的结构（对象/数组）。 */
function isStructured(v: unknown): boolean {
  return v !== null && typeof v === "object";
}

/**
 * 旧版前端 bug 把数组/对象拍平后写回的痕迹（"[object Object]"）。
 *
 * 这种值已经丢了原始结构，无法自动还原；但要**显式告诉用户**，否则用户会以为
 * 配置还在、规则却继续静默失效。留空保存即可（空串跳过，不覆盖后端已有值）。
 */
function looksCollapsed(v: ProbeValue | undefined): boolean {
  return typeof v === "string" && v.includes("[object Object]");
}

/**
 * 这一格是否该用 JSON 编辑器。
 *
 * 三个来源取或：字段声明 `type: "json"`、打开计划时值就是数组/对象
 * （`kinds`），或当前值已是结构。
 *
 * `kinds` 必须参与判定：用户在 textarea 里把内容改成文本后值就变成 string，
 * 只看当前类型会让控件在编辑过程中从多行 textarea 跳回单行 input，
 * 光标与内容当场丢失。渲染与保存用同一个判定，否则会出现「按 JSON 编辑、
 * 却按字符串提交」的不一致。
 */
function isJsonField(
  key: string,
  v: ProbeValue | undefined,
  spec: ProbeFieldSpec | undefined,
  kinds: Record<string, ProbeKind>,
): boolean {
  if (fieldMeta(spec).type === "json") return true;
  if (looksCollapsed(v)) return true;   // 历史损坏值也放进多行框，好让用户重写
  return kinds[key] === "json" || isStructured(v);
}

interface FormState {
  id: string | null;          // null = 新建
  name: string;
  enabled: boolean;
  schedule_mode: "interval" | "daily" | "cron";
  interval_seconds: number;
  time_of_day: string;
  cron: string;
  prompt: string;
  source: string;             // "" = 纯定时
  // 探针参数是任意 JSON（后端 Dict[str, Any]），不能收窄成 string——
  // 那会让数组/对象在编辑时被 String() 拍平（"[object Object]"），
  // 保存时把拍平后的字符串写回后端，依赖该字段的插件静默失效。
  probeConfig: Record<string, ProbeValue>;
  /** 打开计划时各字段原本的类型：控件选择与文本→原生值回写均靠它 */
  probeKinds: Record<string, ProbeKind>;
  permissions: string;
  notifySystem: boolean;
  catch_up: "once" | "all" | "skip";
}

const EMPTY_FORM: FormState = {
  id: null,
  name: "",
  enabled: true,
  schedule_mode: "interval",
  interval_seconds: 3600,
  time_of_day: "09:00",
  cron: "0 9 * * *",
  prompt: "",
  source: "",
  probeConfig: {},
  probeKinds: {},
  permissions: "read",
  notifySystem: false,
  catch_up: "once",
};

/** 常用间隔（秒）——比让用户填秒数友好。 */
const INTERVAL_PRESETS: { label: string; seconds: number }[] = [
  { label: "5 分钟", seconds: 300 },
  { label: "15 分钟", seconds: 900 },
  { label: "30 分钟", seconds: 1800 },
  { label: "1 小时", seconds: 3600 },
  { label: "6 小时", seconds: 21600 },
  { label: "1 天", seconds: 86400 },
];

/** 秒 → 人类可读（列表展示用）。 */
export function humanInterval(seconds: number): string {
  const s = Math.max(60, Math.floor(seconds || 0));
  if (s % 86400 === 0) return `${s / 86400} 天`;
  if (s % 3600 === 0) return `${s / 3600} 小时`;
  if (s % 60 === 0) return `${s / 60} 分钟`;
  return `${s} 秒`;
}

/** 计划的中文摘要（列表副标题）：让用户一眼看懂「多久跑一次、跑什么」。 */
export function describeSchedule(s: ScheduleInfo, sources: WatchSourceInfo[]): string {
  let when: string;
  if (s.schedule_mode === "daily") when = `每天 ${s.time_of_day}`;
  else if (s.schedule_mode === "cron") when = `cron ${s.cron}`;
  else when = `每 ${humanInterval(s.interval_seconds)}`;

  if (!s.source) return `${when} · 定点执行`;
  const src = sources.find((x) => x.source_kind === s.source);
  const srcName = src?.display_name ?? s.source;
  return `${when} · 盯${srcName}（有变化才触发）`;
}

/** 下次运行时间（本地时间字符串；无则不显示）。 */
export function nextRunText(next: number | null | undefined): string {
  if (!next) return "";
  const d = new Date(next * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function ScheduleManager({ sessionId, sources, onChanged }: Props) {
  const [items, setItems] = useState<ScheduleInfo[]>([]);
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const reload = useCallback(async () => {
    try {
      const r = await api.schedules();
      setItems(r.schedules);
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const flash = (m: string) => {
    setNote(m);
    window.setTimeout(() => setNote(""), 2600);
  };

  const selectedSource = useMemo(
    () => sources.find((s) => s.source_kind === form?.source),
    [sources, form?.source],
  );

  const openNew = () => {
    setErr("");
    setForm({ ...EMPTY_FORM });
  };

  const openEdit = (s: ScheduleInfo) => {
    setErr("");
    setForm({
      id: s.id,
      name: s.name,
      enabled: s.enabled,
      schedule_mode: s.schedule_mode,
      interval_seconds: s.interval_seconds,
      time_of_day: s.time_of_day,
      cron: s.cron || "0 9 * * *",
      prompt: s.prompt,
      source: s.source,
      probeConfig: Object.fromEntries(
        Object.entries(s.probe_config ?? {}).map(([k, v]) => {
          // 对象/数组原样保留，交给渲染层决定怎么显示（JSON.stringify 才不丢结构）
          if (isStructured(v)) return [k, v as ProbeValue];
          return [k, v === null || v === undefined ? "" : String(v)];
        }),
      ),
      probeKinds: Object.fromEntries(
        Object.entries(s.probe_config ?? {}).flatMap(([k, v]) => {
          if (isStructured(v)) return [[k, "json" as ProbeKind]];
          if (typeof v === "number") return [[k, "number" as ProbeKind]];
          if (typeof v === "boolean") return [[k, "boolean" as ProbeKind]];
          return [];
        }),
      ),
      permissions: s.permissions,
      notifySystem: (s.notify ?? []).includes("system"),
      catch_up: s.catch_up,
    });
  };

  /**
   * 把编辑器里的探针值归一成要提交的 JSON。
   *
   * 文本框里只有字符串，直接提交会把数组/对象降级成字符串（原来的 bug），
   * 也会把数字/布尔退化成字符串。这里按「声明类型 → 打开时原类型」还原；
   * JSON 非法时报错并**阻止提交**，绝不静默写一个坏值。
   */
  const normalizeProbeConfig = (): Record<string, ProbeValue> | null => {
    if (!form) return null;
    const out: Record<string, ProbeValue> = {};
    for (const [k, v] of Object.entries(form.probeConfig)) {
      const spec = selectedSource?.config_schema?.[k];
      // 与渲染层同一个判定（isJsonField），否则会出现「按 JSON 编辑、却按字符串提交」
      const kind: ProbeKind | "text" = isJsonField(k, v, spec, form.probeKinds)
        ? "json"
        : (fieldMeta(spec).type ?? form.probeKinds[k] ?? "text");

      if (typeof v !== "string") { out[k] = v; continue; }

      const text = v.trim();
      if (text === "") continue;   // 空串视为「未配置」，不写空值覆盖原结构

      if (kind === "json") {
        if (looksCollapsed(v)) {
          // 已损坏（旧 bug 写回的 "[object Object]"），解析必然失败；
          // 给一条能照着做的提示：要么填回正确 JSON，要么清空该框（不改动此字段）
          setErr(`字段「${k}」里是上次保存损坏的内容（[object Object]），`
            + "请填入正确的 JSON 数组/对象；留空保存则不改动该字段");
          return null;
        }
        try {
          out[k] = JSON.parse(text) as ProbeValue;
        } catch {
          setErr(`字段「${k}」不是合法 JSON，请检查后重试`);
          return null;   // 阻止提交
        }
        continue;
      }
      if (kind === "number") {
        const n = Number(text);
        if (Number.isFinite(n)) { out[k] = n; continue; }
        setErr(`字段「${k}」需要是数字`);
        return null;
      }
      if (kind === "boolean") {
        const t = text.toLowerCase();
        if (t === "true" || t === "1") { out[k] = true; continue; }
        if (t === "false" || t === "0") { out[k] = false; continue; }
        setErr(`字段「${k}」需要是 true / false`);
        return null;
      }
      out[k] = v;
    }
    return out;
  };

  const save = async () => {
    if (!form) return;
    if (!form.name.trim()) { setErr("请填写计划名称"); return; }
    if (!form.prompt.trim()) { setErr("请填写触发后要 Agent 做什么"); return; }
    if (form.schedule_mode === "cron" && form.cron.trim().split(/\s+/).length !== 5) {
      setErr("cron 需要 5 段（分 时 日 月 周），如 0 9 * * *");
      return;
    }
    // textarea 里是 JSON 文本，必须解析回真正的数组/对象再提交：
    // 直接把字符串写回后端，插件的 probe_config 解析器会拿不到预期结构（静默失效）。
    const probeConfig = normalizeProbeConfig();
    if (probeConfig === null) return;   // JSON 非法，已给出可操作报错，阻止提交
    const payload: SchedulePayload = {
      name: form.name.trim(),
      enabled: form.enabled,
      schedule_mode: form.schedule_mode,
      interval_seconds: form.interval_seconds,
      time_of_day: form.time_of_day,
      cron: form.cron.trim(),
      prompt: form.prompt.trim(),
      source: form.source,
      probe_config: probeConfig,
      permissions: form.permissions,
      notify: form.notifySystem ? ["in_app", "system"] : ["in_app"],
      catch_up: form.catch_up,
    };
    setBusy(true);
    setErr("");
    try {
      if (form.id) await api.updateSchedule(form.id, payload);
      else await api.createSchedule({ ...payload, session_id: sessionId ?? null });
      setForm(null);
      flash("已保存");
      await reload();
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (s: ScheduleInfo) => {
    try {
      await api.updateSchedule(s.id, { enabled: !s.enabled });
      await reload();
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const remove = async (s: ScheduleInfo) => {
    try {
      await api.deleteSchedule(s.id);
      await reload();
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const runNow = async (s: ScheduleInfo) => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.runSchedule(s.id);
      flash(`已触发：${r.run.detail || r.run.status}`);
      await reload();
      onChanged?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // ---------------- 编辑器 ----------------
  if (form) {
    const probeKeys = Object.keys(selectedSource?.config_schema ?? {});
    return (
      <div className="sched-form">
        <div className="sched-form-head">
          <span className="sched-form-title">{form.id ? "编辑计划" : "新建计划"}</span>
          <button className="sched-btn" onClick={() => setForm(null)}>取消</button>
        </div>

        <label className="sched-field">
          <span>名称</span>
          <input value={form.name} placeholder="如：盯部署状态"
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>

        <label className="sched-field">
          <span>频率</span>
          <select value={form.schedule_mode}
            onChange={(e) => setForm({ ...form, schedule_mode: e.target.value as FormState["schedule_mode"] })}>
            <option value="interval">按间隔</option>
            <option value="daily">每天定时</option>
            <option value="cron">cron 表达式</option>
          </select>
        </label>

        {form.schedule_mode === "interval" && (
          <label className="sched-field">
            <span>间隔</span>
            <select value={form.interval_seconds}
              onChange={(e) => setForm({ ...form, interval_seconds: Number(e.target.value) })}>
              {INTERVAL_PRESETS.map((p) => (
                <option key={p.seconds} value={p.seconds}>{p.label}</option>
              ))}
              {!INTERVAL_PRESETS.some((p) => p.seconds === form.interval_seconds) && (
                <option value={form.interval_seconds}>{humanInterval(form.interval_seconds)}</option>
              )}
            </select>
          </label>
        )}
        {form.schedule_mode === "daily" && (
          <label className="sched-field">
            <span>时间</span>
            <input type="time" value={form.time_of_day}
              onChange={(e) => setForm({ ...form, time_of_day: e.target.value })} />
          </label>
        )}
        {form.schedule_mode === "cron" && (
          <label className="sched-field">
            <span>cron</span>
            <input value={form.cron} placeholder="0 9 * * *"
              onChange={(e) => setForm({ ...form, cron: e.target.value })} />
            <span className="sched-hint">分 时 日 月 周</span>
          </label>
        )}

        <label className="sched-field">
          <span>盯什么</span>
          <select value={form.source} onChange={(e) => {
            setForm({ ...form, source: e.target.value, probeConfig: {}, probeKinds: {} });
          }}>
            <option value="">不盯（定点执行）</option>
            {sources.map((s) => (
              <option key={s.source_kind} value={s.source_kind}>{s.display_name}</option>
            ))}
          </select>
        </label>

        {selectedSource && (
          <>
            <div className="sched-hint">{selectedSource.description}</div>
            {probeKeys.length === 0 && (
              <div className="sched-hint">该数据源不需要额外参数。</div>
            )}
            {probeKeys.map((k) => {
              const spec = selectedSource?.config_schema?.[k];
              const meta = fieldMeta(spec);
              const cur = form.probeConfig[k];
              const asJson = isJsonField(k, cur, spec, form.probeKinds);
              const label = meta.label ?? k;

              // JSON 字段（数组/对象）用多行编辑器：一份价位规则 3 条就 20 多行，
              // 单行 input 既显示成 [object Object] 也没法编辑。
              if (asJson) {
                // 新建时空值显示为空（让 placeholder 提供格式提示），不要把 null 画出来
                const raw = typeof cur === "string" ? cur
                  : cur === undefined || cur === null ? ""
                  : JSON.stringify(cur, null, 2);
                return (
                  <label className="sched-field" key={k}>
                    <span>{label}</span>
                    <textarea
                      className="sched-json"
                      rows={8}
                      spellCheck={false}
                      aria-label={label}
                      value={raw}
                      placeholder={meta.hint}
                      onChange={(e) => setForm({
                        ...form,
                        probeConfig: { ...form.probeConfig, [k]: e.target.value },
                      })}
                    />
                    {meta.hint && <span className="sched-hint">{meta.hint}</span>}
                  </label>
                );
              }

              return (
                <label className="sched-field" key={k}>
                  <span>{label}</span>
                  <input
                    value={typeof cur === "string" ? cur : String(cur ?? "")}
                    placeholder={meta.hint}
                    onChange={(e) => setForm({
                      ...form, probeConfig: { ...form.probeConfig, [k]: e.target.value },
                    })} />
                </label>
              );
            })}
          </>
        )}

        <label className="sched-field">
          <span>触发后</span>
          <textarea rows={3} value={form.prompt} placeholder="告诉 Agent 发现变化后做什么"
            onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
        </label>

        <label className="sched-field">
          <span>权限</span>
          <select value={form.permissions}
            onChange={(e) => setForm({ ...form, permissions: e.target.value })}>
            <option value="read">只读（只看本地，不联网）</option>
            <option value="read+web">只读 + 联网</option>
          </select>
          <span className="sched-hint">
            无人值守运行没人点审批，因此不给写/执行权限（后端同样会拒绝）。
          </span>
        </label>

        <label className="sched-field">
          <span>错过时</span>
          <select value={form.catch_up}
            onChange={(e) => setForm({ ...form, catch_up: e.target.value as FormState["catch_up"] })}>
            <option value="once">补跑一次（推荐）</option>
            <option value="skip">跳过不补</option>
            <option value="all">全部补齐</option>
          </select>
        </label>

        <label className="sched-check">
          <input type="checkbox" checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          <span>启用</span>
        </label>
        <label className="sched-check">
          <input type="checkbox" checked={form.notifySystem}
            onChange={(e) => setForm({ ...form, notifySystem: e.target.checked })} />
          <span>同时发系统通知（「无变化」不会打扰你）</span>
        </label>

        {err && <div className="sched-err">{err}</div>}
        <div className="sched-actions">
          <button className="sched-btn primary" disabled={busy} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    );
  }

  // ---------------- 列表 ----------------
  return (
    <div className="sched-list">
      <div className="sched-list-head">
        <span className="tools-panel-count">计划 {items.length} 个</span>
        {note && <span className="sched-note">{note}</span>}
        <button className="sched-btn primary" onClick={openNew}>+ 新建</button>
      </div>

      {err && <div className="sched-err">{err}</div>}

      {items.length === 0 && !err && (
        <div className="sched-empty">
          还没有盯梢 / 定时计划。<br />
          点「新建」可以定时让 Agent 做一件事，或盯住文件 / 网页，<b>只在有变化时才触发</b>。
        </div>
      )}

      <ul className="sched-items">
        {items.map((s) => (
          <li key={s.id} className={`sched-item ${s.enabled ? "" : "off"}`}>
            <div className="sched-item-head">
              <span className="sched-item-name" title={s.name}>{s.name}</span>
              {s.last_status && (
                <span className={`sched-item-status ${s.last_status}`}>
                  {s.last_status === "error" ? "出错" : s.last_status === "ok" ? "已触发"
                    : s.last_status === "changed" ? "有变化" : s.last_status === "unchanged" ? "无变化"
                    : s.last_status === "skipped" ? "跳过" : s.last_status}
                </span>
              )}
            </div>
            <div className="sched-item-desc">{describeSchedule(s, sources)}</div>
            {s.next_run ? (
              <div className="sched-item-next">下次 ≈ {nextRunText(s.next_run)}</div>
            ) : null}
            <div className="sched-item-ops">
              <button className="sched-op" onClick={() => void toggle(s)}>
                {s.enabled ? "停用" : "启用"}
              </button>
              <button className="sched-op" disabled={busy} onClick={() => void runNow(s)}>立即跑</button>
              <button className="sched-op" onClick={() => openEdit(s)}>编辑</button>
              <button className="sched-op danger" onClick={() => void remove(s)}>删除</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
