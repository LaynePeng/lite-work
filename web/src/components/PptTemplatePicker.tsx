// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// 从模板做 PPT：选版式 → 说需求 → AI 排页写文案 → 生成并直接进编辑器。
//
// 两种模式（顶部切换，默认「AI 生成」）：
//   ai    输入一句话需求（或载入旧稿），后端一次调用排出逐页版式+文案，
//         结果可逐页改文字/换版式/调顺序，然后生成；
//   manual 完全手填占位符（原来的路径，保留给需要精确控制的人）。
//
// 为什么不再是「默认逐页手填」：模板有 20 个版式、每页 2–5 个占位符，
// 一份 10 页的稿子要点 30+ 次输入框，实际没人这么用——用户要的是
// 「一句话出稿，然后微调」，手填只是兜底。
//
// 三个概念必须分开（后端 ppt_templates 已按此建模）：
//   layouts = 模板（能出稿）；brands = 配色（只影响色板）；styles = 规范（给 AI 读）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type PptExtractedSlide,
  type PptImageInfo,
  type PptPlanPage,
  type PptSkillAvailability,
  type PptTemplateBrand,
  type PptTemplateLayout,
  type PptTemplateLibrary,
  type PptTemplatePage,
  type PptTemplateStyle,
} from "../api";

interface Props {
  /** 生成成功后回调：交给上层开编辑器页签 */
  onCreated: (result: { project: string; output: string; slides: number }) => void;
  onCancel?: () => void;
  /** 已有 pptx 路径：非空即「套用模板重排」模式（读旧内容 → 选新设计） */
  sourcePath?: string;
}

/** 占位符 → 中文提示（未列出的按原样显示）。 */
const PLACEHOLDER_HINTS: Record<string, string> = {
  TITLE: "主标题",
  PAGE_TITLE: "页标题",
  SUBTITLE: "副标题",
  CHAPTER_TITLE: "章节标题",
  CHAPTER_DESC: "章节说明",
  CHAPTER_NUM: "章节序号",
  CONTENT_AREA: "正文要点（每行一条）",
  BODY_TEXT: "正文（每行一条）",
  TOP_CONTENT: "上半区内容",
  BOTTOM_CONTENT: "下半区内容",
  KEY_MESSAGE: "核心观点",
  SUPPORT_TEXT: "支撑说明",
  QUOTE_TEXT: "金句原文",
  ATTRIBUTION: "出处",
  CLOSING_MESSAGE: "结束语",
  CTA_TEXT: "行动号召",
  CONTACT_LINE: "联系方式",
  CAPTION: "图注",
  ANNOTATION: "注释",
  SOURCE: "数据来源",
  FOOTER_NOTE: "页脚说明",
  BRAND_LINE: "品牌落款",
  PAGE_NUM: "页码",
  DATE: "日期",
  LEFT_TITLE: "左栏标题",
  LEFT_CONTENT: "左栏内容",
  LEFT_CAPTION: "左栏图注",
  RIGHT_TITLE: "右栏标题",
  RIGHT_CONTENT: "右栏内容",
  RIGHT_CAPTION: "右栏图注",
  KPI_1: "指标 1",
  KPI_2: "指标 2",
  KPI_3: "指标 3",
  KPI_4: "指标 4",
  CARD_1: "卡片 1",
  CARD_2: "卡片 2",
  CARD_3: "卡片 3",
  X_AXIS: "横轴",
  Y_AXIS: "纵轴",
};

/** 长文本占位符用 textarea（正文类天然多行）。 */
const MULTILINE = new Set([
  "CONTENT_AREA", "BODY_TEXT", "TOP_CONTENT", "BOTTOM_CONTENT",
  "LEFT_CONTENT", "RIGHT_CONTENT", "SUPPORT_TEXT", "KEY_MESSAGE",
  "CHAPTER_DESC", "ANNOTATION", "QUOTE_TEXT",
]);

/** 提示词示例：点一下就能改，比空输入框好上手。 */
const EXAMPLES = [
  { label: "季度经营复盘", text: "给管理层做一份 Q3 经营复盘：营收与毛利变化、三个增长点、两个风险、下季度三件事" },
  { label: "产品方案汇报", text: "向客户汇报我们的智能客服方案：行业痛点、方案架构、三个核心能力、落地案例与预期收益" },
  { label: "项目结项", text: "项目结项汇报：目标与达成情况、关键数据、遇到的问题与解法、复盘结论" },
  { label: "培训课件", text: "新员工入职培训：公司业务全景、三个必知流程、常见坑、上手第一周清单" },
];

const PAGE_HINT_OPTIONS = [0, 6, 8, 10, 12, 15];

type Mode = "ai" | "manual";

/** 生成/规划的阶段提示（比一个转圈更能说明在干什么）。 */
type Phase = { kind: "idle" } | { kind: "planning" } | { kind: "generating" };

export default function PptTemplatePicker({ onCreated, onCancel, sourcePath }: Props) {
  const [lib, setLib] = useState<PptTemplateLibrary | null>(null);
  const [suite, setSuite] = useState("");
  const [brand, setBrand] = useState("");
  const [style, setStyle] = useState("");
  const [mode, setMode] = useState<Mode>("ai");
  /** 主题：light（默认）/ dark（色板替换，布局同构） */
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [error, setError] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  /** ppt-master 技能不可用时的说明（null = 正常）。缺它就只能显示空模板库。 */
  const [unavailable, setUnavailable] = useState<PptSkillAvailability | null>(null);

  // —— 单页预览（B1）/ 单页重写（B3）——
  const [previewSvg, setPreviewSvg] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [rewriteInstruction, setRewriteInstruction] = useState("");
  const [rewriting, setRewriting] = useState(false);
  /** 配图能力；null = 还没探测完 */
  const [imageCap, setImageCap] = useState<PptImageInfo | null>(null);
  const [withImages, setWithImages] = useState(false);
  const [imageBackend, setImageBackend] = useState("");

  /** 读模板库（首次加载与「重新检测」共用）。 */
  const loadLibrary = useCallback(async () => {
    setError("");
    try {
      const data = await api.pptTemplates();
      // 技能缺失时后端返回空 layouts + available.ready=false：这时必须走
      // 「安装/修复」提示，而不是把空模板库当成正常状态（否则页面永远空白）
      if (data.available && !data.available.ready) {
        setUnavailable(data.available);
        setLib(data);
        return;
      }
      setUnavailable(null);
      setLib(data);
      const first = data.layouts[0];
      if (first) {
        setSuite(first.suite);
        setPages([{ page: first.pages[0]?.file ?? "", values: {} }]);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  /** 「重新检测」：修复安装后不必重开页签。 */
  const retryAvailability = useCallback(() => {
    setLib(null);
    setUnavailable(null);
    void loadLibrary();
  }, [loadLibrary]);

  // —— AI 模式 ——
  const [brief, setBrief] = useState("");
  const [audience, setAudience] = useState("");
  const [pageHint, setPageHint] = useState(0);
  /** 规划产物；null = 还没规划过 */
  const [planned, setPlanned] = useState<PptPlanPage[] | null>(null);
  const [planNote, setPlanNote] = useState("");
  /** 规划后手动改过的页（值是 values 覆盖） */
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({});
  const [activeIdx, setActiveIdx] = useState(0);

  // —— 手动模式 ——
  const [pages, setPages] = useState<{ page: string; values: Record<string, string> }[]>([]);

  // —— 重排模式 ——
  const [extracted, setExtracted] = useState<PptExtractedSlide[] | null>(null);
  /** 重排请求只跑一次：成功/失败都由 planned 接管后续界面 */
  const restyleStarted = useRef(false);

  const busy = phase.kind !== "idle";

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  // 配图能力：生图（需供应商密钥）或图库（零密钥兜底）
  useEffect(() => {
    void (async () => {
      try {
        const r = await api.pptImages();
        setImageCap(r);
        setImageBackend((cur) => cur || r.default_backend || r.backends[0]?.backend || "");
      } catch {
        // 探测失败不阻断模板页：配图是增强项
      }
    })();
  }, []);

  // 重排模式：进来就读源 pptx 的内容结构
  useEffect(() => {
    if (!sourcePath) return;
    void (async () => {
      try {
        const r = await api.pptExtract(sourcePath);
        setExtracted(r.slides);
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, [sourcePath]);

  const layout: PptTemplateLayout | undefined = useMemo(
    () => lib?.layouts.find((l) => l.suite === suite),
    [lib, suite],
  );
  const pageOf = useCallback(
    (file: string): PptTemplatePage | undefined => layout?.pages.find((p) => p.file === file),
    [layout],
  );
  /** AI 模式：页标识（slug）→ 模板页元数据。 */
  const pageBySlug = useCallback(
    (slug: string): PptTemplatePage | undefined =>
      layout?.pages.find((p) => p.slug === slug || p.file === slug),
    [layout],
  );

  const pickSuite = (s: string) => {
    setSuite(s);
    const l = lib?.layouts.find((x) => x.suite === s);
    setPages([{ page: l?.pages[0]?.file ?? "", values: {} }]);
    // 换套件后旧版式标识不再通用，规划结果作废（避免生成时报「找不到版式」）
    setPlanned(null);
    setEdits({});
    setPlanNote("");
    setActiveIdx(0);
  };

  // ---------------------------------------------------------------- 规划

  const runPlan = useCallback(async (opts?: { restyle?: boolean }) => {
    if (!suite) return;
    const text = brief.trim();
    if (!opts?.restyle && !text && !sourcePath) {
      setError("先说一句要做什么，比如「给管理层做 Q3 经营复盘」");
      return;
    }
    setError("");
    setPlanNote("");
    setPhase({ kind: "planning" });
    try {
      const r = await api.pptPlan({
        suite,
        ...(text ? { brief: text } : {}),
        ...(audience.trim() ? { audience: audience.trim() } : {}),
        ...(pageHint ? { page_hint: pageHint } : {}),
        ...(sourcePath ? { source_path: sourcePath } : {}),
        ...(withImages ? { with_images: true } : {}),
      });
      setPlanned(r.pages);
      setEdits({});
      setActiveIdx(0);
      setMode("ai");
      if (r.fallback) {
        setPlanNote(
          `AI 没能给出可用方案（${r.error ?? "未知原因"}），已按版式排好 ${r.pages.length} 页骨架，`
          + "文案留空可直接编辑或重新规划。");
      } else {
        setPlanNote(`已按需求排好 ${r.pages.length} 页，可逐页改文案/换版式，满意后生成。`);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPhase({ kind: "idle" });
    }
  }, [suite, brief, audience, pageHint, sourcePath, withImages]);

  // 重排模式：源结构读出来后自动规划一次（用户点进来就是要重排，不该再点一次）
  useEffect(() => {
    if (!sourcePath || !extracted?.length || !suite || restyleStarted.current) return;
    restyleStarted.current = true;
    void runPlan({ restyle: true });
  }, [sourcePath, extracted, suite, runPlan]);

  // ---------------------------------------------------------------- 生成

  /** 规划结果 + 用户编辑 → create 需要的 pages（页标识归一成模板文件名）。 */
  const plannedSpec = useCallback(() => {
    if (!planned) return null;
    return {
      suite,
      pages: planned.map((p) => {
        const meta = pageBySlug(p.page);
        const file = meta?.file ?? p.page;
        return { page: file, values: { ...p.values, ...(edits[p.page] ?? {}) } };
      }),
    };
  }, [planned, suite, pageBySlug, edits]);

  const generate = async () => {
    const spec = mode === "ai" ? plannedSpec() : { suite, brand, style, pages };
    if (!spec || !spec.pages.length) {
      setError("还没有可生成的页面");
      return;
    }
    setError("");
    setPhase({ kind: "generating" });
    try {
      // 配图：把规划好的 image_prompts 收集成「页码:图片位」→ 提示词。
      // 换了版式的页可能没有对应图片位，后端会按图片位白名单过滤，这里不筛。
      const imgSpec: Record<string, string> = {};
      if (withImages && planned) {
        planned.forEach((p, i) => {
          for (const [slot, prompt] of Object.entries(p.image_prompts ?? {})) {
            if (prompt.trim()) imgSpec[`${i + 1}:${slot}`] = prompt;
          }
        });
      }
      const hasImages = Object.keys(imgSpec).length > 0;
      const r = await api.pptCreate(
        { suite: spec.suite, ...(brand ? { brand } : {}),
          ...(style ? { style } : {}), ...(theme === "dark" ? { theme: "dark" } : {}),
          pages: spec.pages },
        undefined,
        hasImages
          ? {
              spec: imgSpec,
              ...(imageBackend ? { backend: imageBackend } : {}),
              // 明确告诉后端走哪条通道，避免「没有生图凭据时静默跳过配图」
              ...(imageCap?.mode && imageCap.mode !== "none" ? { mode: imageCap.mode } : {}),
            }
          : undefined,
      );
      if (r.warnings?.length) {
        setPlanNote(`已生成，但有提示：${r.warnings.join("；")}`);
      }
      onCreated({ project: r.project, output: r.output, slides: r.slides });
    } catch (e) {
      setError((e as Error).message);
      setPhase({ kind: "idle" });
    }
  };

  /** 规划结果里某一页的文案（含用户改动）。 */
  const valueOf = (p: PptPlanPage, key: string): string => edits[p.page]?.[key] ?? p.values[key] ?? "";

  /** 单页预览：用当前（含编辑）文案渲染这一页的 SVG（B1）。 */
  const loadPreview = useCallback(async (page: PptPlanPage) => {
    if (!suite || !page) return;
    setPreviewLoading(true);
    try {
      const values: Record<string, string> = { ...page.values, ...(edits[page.page] ?? {}) };
      const r = await api.pptPreview({ suite, page: page.page, values });
      setPreviewSvg(r.ok ? r.svg : "");
    } catch {
      setPreviewSvg("");     // 预览失败不阻断：编辑区照常可用
    } finally {
      setPreviewLoading(false);
    }
  }, [suite, edits]);

  // 当前编辑页变化时刷新预览（防抖：连续打字不必每次都请求）
  useEffect(() => {
    const p = planned?.[activeIdx];
    if (!p) return;
    const t = window.setTimeout(() => void loadPreview(p), 400);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIdx, planned, edits]);

  /** 单页重写（B3）：只让模型改这一页的文案。 */
  const rewriteCurrentPage = useCallback(async () => {
    const p = planned?.[activeIdx];
    if (!p || !rewriteInstruction.trim()) return;
    setRewriting(true);
    setError("");
    try {
      const r = await api.pptRewritePage({
        suite, page: p.page,
        values: { ...p.values, ...(edits[p.page] ?? {}) },
        instruction: rewriteInstruction.trim(),
      });
      // 重写结果并入编辑层（不动规划原值，可继续手改）
      setEdits((prev) => ({ ...prev, [p.page]: { ...(prev[p.page] ?? {}), ...r.values } }));
      setRewriteInstruction("");
      setPlanNote("已重写这一页，可继续手改或重新生成");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRewriting(false);
    }
  }, [suite, planned, activeIdx, edits, rewriteInstruction]);
  const setPlannedValue = (p: PptPlanPage, key: string, val: string) =>
    setEdits((prev) => ({ ...prev, [p.page]: { ...(prev[p.page] ?? {}), [key]: val } }));

  const movePlanned = (i: number, dir: -1 | 1) => setPlanned((prev) => {
    if (!prev) return prev;
    const j = i + dir;
    if (j < 0 || j >= prev.length) return prev;
    const next = [...prev];
    [next[i], next[j]] = [next[j], next[i]];
    setActiveIdx(j);
    return next;
  });
  const removePlanned = (i: number) => setPlanned((prev) => {
    if (!prev) return prev;
    const next = prev.filter((_, idx) => idx !== i);
    setActiveIdx(Math.max(0, Math.min(activeIdx, next.length - 1)));
    return next;
  });
  /** 换某页版式：保留同名占位符的文案，其余丢弃。 */
  const swapPlannedLayout = (i: number, slug: string) => setPlanned((prev) => {
    if (!prev) return prev;
    const meta = pageBySlug(slug);
    if (!meta) return prev;
    const keep: Record<string, string> = {};
    const old = prev[i];
    for (const ph of meta.placeholders) {
      const v = old.values[ph];
      if (v) keep[ph] = v;
    }
    const next = [...prev];
    next[i] = { ...old, page: meta.slug, label: meta.label, values: keep };
    return next;
  });

  // ---------------------------------------------------------------- 手动模式

  const applyPreset = (slugs: string[]) => {
    if (!layout) return;
    const next: { page: string; values: Record<string, string> }[] = [];
    for (const slug of slugs) {
      const hit = layout.pages.find((p) => p.slug === slug)
        ?? layout.pages.find((p) => p.slug.includes(slug));
      if (hit) next.push({ page: hit.file, values: {} });
    }
    if (next.length) setPages(next);
  };
  const addPage = (file: string) => setPages((p) => [...p, { page: file, values: {} }]);
  const removePage = (i: number) => setPages((p) => p.filter((_, idx) => idx !== i));
  const movePage = (i: number, dir: -1 | 1) => setPages((p) => {
    const j = i + dir;
    if (j < 0 || j >= p.length) return p;
    const next = [...p];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  });
  const setValue = (i: number, key: string, val: string) => setPages((p) =>
    p.map((item, idx) => (idx === i ? { ...item, values: { ...item.values, [key]: val } } : item)));

  // ---------------------------------------------------------------- 渲染

  if (!lib) {
    return (
      <div className="ppt-tpl">
        <div className="ppt-tpl-loading">{error || "正在读取模板库…"}</div>
      </div>
    );
  }

  // ppt-master 不可用：明确说清「缺什么、怎么修」，并给重试入口。
  // 不能退回「空模板库」——那会让用户以为模板用完了。
  if (unavailable) {
    return (
      <div className="ppt-tpl">
        <div className="ppt-tpl-head">
          <div className="ppt-tpl-head-main">
            <h3>{sourcePath ? "套用模板重排" : "从模板做 PPT"}</h3>
            <p>依赖内置技能 ppt-master</p>
          </div>
          {onCancel && <button className="btn" onClick={onCancel}>取消</button>}
        </div>
        <div className="ppt-tpl-main">
          <div className="ppt-tpl-unavailable">
            <div className="ppt-tpl-unavailable-icon" aria-hidden>🧩</div>
            <h4>PPT 模板能力不可用：缺少内置技能 ppt-master</h4>
            <p className="ppt-tpl-unavailable-reason">{unavailable.reason}</p>
            <p className="ppt-tpl-unavailable-hint">{unavailable.hint}</p>

            <ul className="ppt-tpl-unavailable-checks">
              <li className={unavailable.skill_dir ? "ok" : "bad"}>
                <span className="ppt-tpl-check-dot" />
                技能目录{unavailable.skill_dir ? <>：<code>{unavailable.skill_dir}</code></> : "（未找到）"}
              </li>
              <li className={unavailable.layouts > 0 ? "ok" : "bad"}>
                <span className="ppt-tpl-check-dot" />
                模板资产：{unavailable.layouts} 套版式
              </li>
              <li className={unavailable.scripts ? "ok" : "bad"}>
                <span className="ppt-tpl-check-dot" />
                转换脚本 pptx_to_svg.py / svg_to_pptx.py
              </li>
            </ul>

            <div className="ppt-tpl-unavailable-actions">
              <button className="btn primary" onClick={retryAvailability}>重新检测</button>
            </div>

            <p className="ppt-tpl-unavailable-note">
              ppt-master 是<b>随安装包分发的内置技能</b>，不在插件市场里，
              所以不需要单独安装——缺它通常是安装包不完整或被误删。
              由 AI 直接生成 PPT（不套模板）走对话页签即可，不依赖本技能。
            </p>
          </div>
        </div>
      </div>
    );
  }

  const pageCount = mode === "ai" ? (planned?.length ?? 0) : pages.length;
  const currentPlanned = planned?.[activeIdx];

  /** 配图总开关是否可用：有生图模型 or 有图库（图库零密钥，几乎总有）。 */
  const canImage = !!imageCap && (imageCap.can_generate || imageCap.can_stock);
  /** 一句话说清「这次会用哪种配图」——两者能力差别大，必须讲明白。 */
  const imageHint = !imageCap
    ? "正在检测配图能力…"
    : imageCap.mode === "generate"
      ? "带图片位的版式会按内容生成配图（复用已配置的供应商密钥）"
      : imageCap.mode === "stock"
        ? "未配置生图模型：将从开放图库检索配图（免密钥，可能需署名）"
        : "既没有生图凭据也没有可用图库，无法配图";

  return (
    <div className="ppt-tpl">
      {/* —— 头部：标题 + 模式切换 —— */}
      <div className="ppt-tpl-head">
        <div className="ppt-tpl-head-main">
          <h3>{sourcePath ? "套用模板重排" : "从模板做 PPT"}</h3>
          <p>
            {sourcePath
              ? <>源文件 <code>{sourcePath}</code> · {extracted?.length ?? "…"} 页，AI 正在按新版式重排</>
              : "选一套版式，说一句话，AI 排页写文案，生成后直接在编辑器里微调"}
          </p>
        </div>
        {!sourcePath && (
          <div className="ppt-tpl-modes" role="tablist" aria-label="生成方式">
            <button role="tab" aria-selected={mode === "ai"}
                    className={mode === "ai" ? "on" : ""} onClick={() => setMode("ai")}>
              ✨ AI 生成
            </button>
            <button role="tab" aria-selected={mode === "manual"}
                    className={mode === "manual" ? "on" : ""} onClick={() => setMode("manual")}>
              手动编排
            </button>
          </div>
        )}
        {onCancel && <button className="btn" onClick={onCancel}>取消</button>}
      </div>

      {error && (
        <div className="ppt-editor-error" onClick={() => setError("")} title="点击关闭">
          {error}
        </div>
      )}

      <div className="ppt-tpl-body">
        {/* —— 左：套件 / 配色 / 风格 —— */}
        <div className="ppt-tpl-side">
          <label className="ppt-tpl-label">版式套件</label>
          <div className="ppt-tpl-list">
            {lib.layouts.map((l) => (
              <button key={l.suite}
                      className={`ppt-tpl-item ${l.suite === suite ? "on" : ""}`}
                      onClick={() => pickSuite(l.suite)}>
                <span className="ppt-tpl-item-t">{l.label}</span>
                <span className="ppt-tpl-item-s">{l.page_count} 页</span>
              </button>
            ))}
          </div>

          <label className="ppt-tpl-label">品牌配色</label>
          <div className="ppt-tpl-chips">
            <button className={`ppt-chip ${brand === "" ? "on" : ""}`} onClick={() => setBrand("")}>默认</button>
            {lib.brands.map((b: PptTemplateBrand) => (
              <button key={b.key}
                      className={`ppt-chip ${brand === b.key ? "on" : ""}`}
                      title={b.summary}
                      onClick={() => setBrand(b.key)}>
                <span className="ppt-chip-dot" style={{ background: b.primary_color || "#888" }} />
                {b.label}
              </button>
            ))}
          </div>

          <label className="ppt-tpl-label">设计风格</label>
          <div className="ppt-tpl-chips">
            <button className={`ppt-chip ${style === "" ? "on" : ""}`} onClick={() => setStyle("")}>默认</button>
            {lib.styles.map((s: PptTemplateStyle) => (
              <button key={s.key}
                      className={`ppt-chip ${style === s.key ? "on" : ""}`}
                      title={s.summary}
                      onClick={() => setStyle(s.key)}>
                {s.label}
              </button>
            ))}
          </div>

          <label className="ppt-tpl-label">主题</label>
          <div className="ppt-tpl-chips">
            <button className={`ppt-chip ${theme === "light" ? "on" : ""}`}
                    onClick={() => setTheme("light")}>☀️ 浅色</button>
            <button className={`ppt-chip ${theme === "dark" ? "on" : ""}`}
                    onClick={() => setTheme("dark")}
                    title="同一版式的深色变体：色板替换、布局与字号不变">🌙 深色</button>
          </div>
        </div>

        {/* —— 右：主工作区 —— */}
        <div className="ppt-tpl-main">
          {/* 提示词区：AI 模式且尚未规划时占据主视觉 */}
          {mode === "ai" && (
            <div className={`ppt-tpl-brief ${planned ? "compact" : ""}`}>
              <div className="ppt-tpl-brief-row">
                <span className="ppt-tpl-label">
                  {sourcePath ? "补充要求（可选，留空则按源稿内容重排）" : "要做什么 PPT"}
                </span>
                {planned && (
                  <button className="ppt-tpl-link" onClick={() => { setPlanned(null); setPlanNote(""); }}>
                    {sourcePath ? "清空方案重排" : "重新描述需求"}
                  </button>
                )}
              </div>
              <textarea
                className="ppt-tpl-brief-input"
                rows={planned ? 2 : 4}
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
                placeholder={sourcePath
                  ? "可选：补充重排要求，例如「保留原来的数据页，讲给客户看，语气更克制」"
                  : "一句话说明主题、受众、要讲的重点。例：给管理层做 Q3 经营复盘，重点是营收与毛利变化、三个增长点、两个风险和下季度三件事"}
              />
              {!planned && (
                <>
                  <div className="ppt-tpl-examples">
                    <span className="ppt-tpl-label">试试</span>
                    {EXAMPLES.map((ex) => (
                      <button key={ex.label} className="ppt-chip" title={ex.text}
                              onClick={() => setBrief(ex.text)}>
                        {ex.label}
                      </button>
                    ))}
                  </div>
                  <div className="ppt-tpl-brief-row wrap">
                    <label className="ppt-tpl-inline">
                      受众/场合
                      <input value={audience} onChange={(e) => setAudience(e.target.value)}
                             placeholder="如 管理层 / 客户 / 新人" />
                    </label>
                    <label className="ppt-tpl-inline">
                      页数
                      <select value={pageHint} onChange={(e) => setPageHint(Number(e.target.value))}>
                        {PAGE_HINT_OPTIONS.map((n) => (
                          <option key={n} value={n}>{n === 0 ? "交给 AI" : `${n} 页`}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  {/* 配图：按能力给不同选项——没有生图模型时图库仍可用 */}
                  <div className="ppt-tpl-imagerow">
                    <label className={`ppt-tpl-toggle ${canImage ? "" : "disabled"}`}>
                      <input type="checkbox" checked={withImages} disabled={!canImage}
                             onChange={(e) => setWithImages(e.target.checked)} />
                      <span>{imageCap?.mode === "generate" ? "用生图模型配图" : "自动配图"}</span>
                    </label>
                    {withImages && (imageCap?.backends.length ?? 0) > 1 && (
                      <select className="ppt-tpl-inline-select" value={imageBackend}
                              onChange={(e) => setImageBackend(e.target.value)}>
                        {imageCap!.backends.map((b) => (
                          <option key={b.backend} value={b.backend}>{b.label}</option>
                        ))}
                      </select>
                    )}
                    <span className="ppt-tpl-imagehint">{imageHint}</span>
                  </div>
                </>
              )}
            </div>
          )}

          {planNote && <div className="ppt-tpl-note">{planNote}</div>}

          {/* AI 模式：规划后的逐页编辑 */}
          {mode === "ai" && planned && currentPlanned && (
            <>
              <div className="ppt-tpl-planbar">
                <span className="ppt-tpl-label">逐页方案（{planned.length} 页）</span>
                <span className="ppt-tpl-planbar-ops">
                  <button className="ppt-tpl-link" onClick={() => void runPlan()}
                          disabled={busy} title="按当前需求重新排一遍">
                    重新规划
                  </button>
                </span>
              </div>

              <div className="ppt-tpl-plan">
                {/* 左：页列表 */}
                <div className="ppt-tpl-plan-list">
                  {planned.map((p, i) => (
                    <button key={`${p.page}-${i}`}
                            className={`ppt-tpl-plan-item ${i === activeIdx ? "on" : ""}`}
                            onClick={() => setActiveIdx(i)}>
                      <span className="ppt-tpl-page-n">{i + 1}</span>
                      <span className="ppt-tpl-plan-item-body">
                        <span className="ppt-tpl-plan-item-t">
                          {p.values.TITLE || p.values.PAGE_TITLE || p.values.CHAPTER_TITLE || p.label}
                        </span>
                        <span className="ppt-tpl-plan-item-s">{p.label}</span>
                      </span>
                    </button>
                  ))}
                </div>

                {/* 右：单页文案编辑 */}
                <div className="ppt-tpl-plan-editor">
                  {/* B1 预览：所见即所得——文字列表看不出版式效果 */}
                  {previewSvg && (
                    <div className="ppt-tpl-preview"
                         dangerouslySetInnerHTML={{ __html: previewSvg }} />
                  )}
                  {previewLoading && <div className="ppt-tpl-preview-loading">渲染中…</div>}
                  <div className="ppt-tpl-page-head">
                    <span className="ppt-tpl-page-n">{activeIdx + 1}</span>
                    <select value={currentPlanned.page}
                            onChange={(e) => swapPlannedLayout(activeIdx, e.target.value)}
                            title="换这一页的版式">
                      {(layout?.pages ?? []).map((p) => (
                        <option key={p.file} value={p.slug}>{p.label}</option>
                      ))}
                    </select>
                    <span className="ppt-tpl-page-ops">
                      <button onClick={() => movePlanned(activeIdx, -1)} disabled={activeIdx === 0}
                              title="上移">↑</button>
                      <button onClick={() => movePlanned(activeIdx, 1)}
                              disabled={activeIdx === planned.length - 1} title="下移">↓</button>
                      <button onClick={() => removePlanned(activeIdx)} title="删除本页">✕</button>
                    </span>
                  </div>
                  {/* B3 单页重写：一句话让 AI 只改这页 */}
                  <div className="ppt-tpl-rewrite">
                    <input
                      value={rewriteInstruction}
                      onChange={(e) => setRewriteInstruction(e.target.value)}
                      placeholder="AI 重写本页：如「标题更犀利，正文改成三条要点」"
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void rewriteCurrentPage();
                        }
                      }}
                    />
                    <button className="btn" onClick={() => void rewriteCurrentPage()}
                            disabled={rewriting || !rewriteInstruction.trim()}>
                      {rewriting ? "重写中…" : "✨ 重写本页"}
                    </button>
                  </div>
                  <div className="ppt-tpl-fields">
                    {(pageBySlug(currentPlanned.page)?.placeholders
                      ?? Object.keys(currentPlanned.values)).map((ph) => (
                      <label key={ph} className={MULTILINE.has(ph) ? "wide" : ""}>
                        <span>{PLACEHOLDER_HINTS[ph] ?? ph}</span>
                        {MULTILINE.has(ph)
                          ? <textarea rows={ph === "CONTENT_AREA" || ph === "BODY_TEXT" ? 5 : 3}
                                      value={valueOf(currentPlanned, ph)}
                                      onChange={(e) => setPlannedValue(currentPlanned, ph, e.target.value)} />
                          : <input value={valueOf(currentPlanned, ph)}
                                   onChange={(e) => setPlannedValue(currentPlanned, ph, e.target.value)} />}
                      </label>
                    ))}
                    {/* 配图提示词：让用户看得见 AI 打算画什么，也可自己改 */}
                    {withImages && Object.keys(currentPlanned.image_prompts ?? {}).length > 0 && (
                      <div className="wide ppt-tpl-imgprompts">
                        <span className="ppt-tpl-imgprompts-head">🎨 本页配图（生成时自动出图）</span>
                        {Object.entries(currentPlanned.image_prompts ?? {}).map(([slot, prompt]) => (
                          <label key={slot}>
                            <span>{slot} · 画面描述（英文）</span>
                            <textarea rows={2} value={prompt} onChange={(e) => {
                              const val = e.target.value;
                              setPlanned((prev) => prev ? prev.map((pp, idx) => idx === activeIdx
                                ? { ...pp, image_prompts: { ...(pp.image_prompts ?? {}), [slot]: val } }
                                : pp) : prev);
                            }} />
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </>
          )}

          {/* 手动模式：原来的逐页填 */}
          {mode === "manual" && (
            <>
              <div className="ppt-tpl-presets">
                <span className="ppt-tpl-label">快速结构</span>
                <button className="ppt-chip" title="封面 + 2 页内容"
                        onClick={() => applyPreset(["title_slide", "title_content", "title_content"])}>
                  精简汇报
                </button>
                <button className="ppt-chip" title="封面/章节/内容/数据/金句"
                        onClick={() => applyPreset(["title_slide", "section_header", "title_content",
                                                    "kpi_dashboard", "title_content", "hero_statement"])}>
                  完整结构
                </button>
                <button className="ppt-chip" title="只出一张封面"
                        onClick={() => applyPreset(["title_slide"])}>
                  只要封面
                </button>
              </div>

              <div className="ppt-tpl-pages">
                {pages.map((item, i) => {
                  const meta = pageOf(item.page);
                  return (
                    <div className="ppt-tpl-page" key={`${item.page}-${i}`}>
                      <div className="ppt-tpl-page-head">
                        <span className="ppt-tpl-page-n">{i + 1}</span>
                        <span className="ppt-tpl-page-name">{meta?.label ?? item.page}</span>
                        <span className="ppt-tpl-page-ops">
                          <button onClick={() => movePage(i, -1)} disabled={i === 0} title="上移">↑</button>
                          <button onClick={() => movePage(i, 1)} disabled={i === pages.length - 1}
                                  title="下移">↓</button>
                          <button onClick={() => removePage(i)} title="删除">✕</button>
                        </span>
                      </div>
                      <div className="ppt-tpl-fields">
                        {(meta?.placeholders ?? []).map((ph) => (
                          <label key={ph} className={MULTILINE.has(ph) ? "wide" : ""}>
                            <span>{PLACEHOLDER_HINTS[ph] ?? ph}</span>
                            {MULTILINE.has(ph)
                              ? <textarea rows={3} value={item.values[ph] ?? ""}
                                          onChange={(e) => setValue(i, ph, e.target.value)}
                                          placeholder={PLACEHOLDER_HINTS[ph]} />
                              : <input value={item.values[ph] ?? ""}
                                       onChange={(e) => setValue(i, ph, e.target.value)}
                                       placeholder={PLACEHOLDER_HINTS[ph]} />}
                          </label>
                        ))}
                        {!meta?.placeholders.length && <span className="ppt-tpl-empty">该页无占位符</span>}
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="ppt-tpl-add">
                <span className="ppt-tpl-label">添加页面</span>
                <div className="ppt-tpl-chips">
                  {(layout?.pages ?? []).map((p) => (
                    <button key={p.file} className="ppt-chip" onClick={() => addPage(p.file)}>
                      + {p.label}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}

          {/* 重排模式：源内容结构（只读，作为规划的输入依据） */}
          {sourcePath && (
            <>
              <div className="ppt-tpl-planbar">
                <span className="ppt-tpl-label">源内容结构（AI 将按此重排）</span>
              </div>
              <div className="ppt-tpl-pages">
                {!extracted && <div className="ppt-tpl-loading">正在读取源文件…</div>}
                {(extracted ?? []).map((sl) => (
                  <div className="ppt-tpl-page" key={sl.slide}>
                    <div className="ppt-tpl-page-head">
                      <span className="ppt-tpl-page-n">{sl.slide}</span>
                      <span className="ppt-tpl-page-name">{sl.title || "（无标题）"}</span>
                      <span className="ppt-tpl-page-tags">
                        <span className="ppt-tpl-tag">{sl.layout_guess}</span>
                        {sl.has_table && <span className="ppt-tpl-tag t">表格</span>}
                        {sl.has_chart && <span className="ppt-tpl-tag c">图表</span>}
                        {sl.has_picture && <span className="ppt-tpl-tag i">图片</span>}
                      </span>
                    </div>
                    <div className="ppt-tpl-fields">
                      {sl.paragraphs.length > 0 ? (
                        <div className="wide ppt-tpl-paras">
                          {sl.paragraphs.slice(0, 6).map((pa, i) => (
                            <div key={i}>· {pa}</div>
                          ))}
                          {sl.paragraphs.length > 6 && (
                            <div className="more">… 共 {sl.paragraphs.length} 段</div>
                          )}
                        </div>
                      ) : (
                        <span className="ppt-tpl-empty">
                          {sl.has_table ? "表格页" : sl.has_chart ? "图表页" : "无文本段落"}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* —— 动作栏：固定在滚动区之外 —— */}
      <div className="ppt-tpl-actions">
        <span className="ppt-tpl-actions-hint">
          {phase.kind === "planning" ? "AI 正在排页与写文案…"
            : phase.kind === "generating" ? `正在生成 ${pageCount} 页…`
            : mode === "ai"
              ? (planned ? `将生成 ${pageCount} 页 · ${layout?.label ?? ""}` : "先规划，再生成")
              : `将生成 ${pageCount} 页 · ${layout?.label ?? ""}`}
        </span>
        {phase.kind === "planning" && <span className="ppt-tpl-spinner" aria-hidden />}
        {/* 主次分明：AI 模式下未规划时主按钮是「规划」，规划后才是「生成」。
            重排模式允许不带需求（按源稿内容直接重排），所以门槛看「有没有源文件」。 */}
        {mode === "ai" && !planned ? (
          <button className="btn primary"
                  disabled={busy || (!brief.trim() && !sourcePath)}
                  onClick={() => void runPlan()}>
            {phase.kind === "planning" ? "规划中…" : sourcePath ? "✨ 重排方案" : "✨ 规划方案"}
          </button>
        ) : (
          <button className="btn primary" disabled={busy || !pageCount} onClick={() => void generate()}>
            {phase.kind === "generating" ? "生成中…" : "生成并打开编辑器"}
          </button>
        )}
      </div>
    </div>
  );
}
