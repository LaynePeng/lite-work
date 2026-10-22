// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 lite-work contributors
//
// PPT 编辑器页签：三栏布局，人直接在界面上改已有 PPT。
//
// 为什么做成「页签」而不是气泡里的富块：
// 气泡内容区定宽 820px、单条消息再被 .bubble 的 76% 收窄到约 623px，
// 而 16:9 幻灯片是 1280×720——塞进气泡只能缩到 1:2，正文 6px 高，
// 元素点不中、属性面板无处安放。做成 kind="ppt" 的页签后：
//   1) 占满 .main，画布拿到全部剩余宽度；
//   2) 右侧工具面板按 activeTab.kind === "chat" 判断，自动不渲染，
//      不必新造布局机制（App.tsx 既有行为）。
//
// 三区分工：左 = 幻灯片轨（复用侧栏竖长比例），中 = 画布，右 = 属性/标注。
// 右侧面板只在选中元素时滑出，浏览态画布保持满宽。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, type PptAnnotation, type PptElement, type PptOutline, type PptSlide } from "../api";
import { rewriteLocalUrls } from "./RichBlocks";

/** 画布像素 → 磅值（ppt-master 用 96dpi 映射幻灯片英寸尺寸）。 */
const pxToPt = (px: number | null | undefined): number | null =>
  typeof px === "number" ? Math.round((px * 3) / 4 * 10) / 10 : null;
const ptToPx = (pt: number): number => (pt * 4) / 3;

type InspectorTab = "props" | "annotate";

/** 未提交草稿的跨卸载缓存：key = 工作区名（project）。
 *  点「交 AI 应用」会切到对话页签、编辑器随之卸载，草稿不能跟着丢。 */
const DRAFT_CACHE = new Map<string, Record<string, Record<string, unknown>>>();

interface Props {
  project: string;
  source: string;
  /** 「应用改动」：把标注清单编码成一条消息回传给会话，由 Agent 执行 */
  onRequestApply?: (payload: { project: string; source: string; annotations: PptAnnotation[] }) => void;
}

/** 内联渲染单页 SVG，并在其上叠加可点选的热区。
 *
 *  为什么不用 iframe：sandbox iframe 取不到 DOM，点选/高亮/热区全都做不了。
 *  内联后 SVG 成为 React 树的一部分，用一层绝对定位的透明热区接管点击，
 *  既避免与 SVG 内的文字选择冲突，也不改动 SVG 本身（导出仍走原文件）。
 */
export default function PptEditor({ project, source, onRequestApply }: Props) {
  const [outline, setOutline] = useState<PptOutline | null>(null);
  const [slideIndex, setSlideIndex] = useState(1);
  const [svg, setSvg] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<InspectorTab>("props");
  const [annotationDraft, setAnnotationDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  /** 演讲者备注（B4）：当前页的备注草稿 + 面板开关 */
  const [noteOpen, setNoteOpen] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ w: 1280, h: 720 });

  /** 草稿态：连续微调只进内存，画布即时预览，点「提交」才写盘。
   *
   *  为什么需要：此前每次改属性都要「写 SVG → 跑 Python 刷新 summary →
   *  重拉全量 outline → 重载 SVG」两趟子进程，改一个字号要等好几秒且画面
   *  闪一下。连续调字号/拖位置时体验尤其糟。这里把同步点后移，
   *  key = `${slide}:${elementId}`，value = 累积的 changes。
   *
   *  草稿还按工作区缓存到模块级：点「交 AI 应用」会切到对话页签、编辑器卸载，
   *  切回来时不能把用户攒的未提交改动一起清掉。
   */
  const [drafts, setDrafts] = useState<Record<string, Record<string, unknown>>>(
    () => DRAFT_CACHE.get(project) ?? {},
  );
  useEffect(() => { DRAFT_CACHE.set(project, drafts); }, [project, drafts]);
  const [zoom, setZoom] = useState<number | null>(null);   // null = 适应宽度
  /** 每页缩略 SVG（左栏预览用），key = 页码。 */
  const [thumbs, setThumbs] = useState<Record<number, string>>({});
  const draftCount = Object.keys(drafts).length;

  const current: PptSlide | undefined = useMemo(
    () => outline?.slides.find((s) => s.slide === slideIndex),
    [outline, slideIndex],
  );
  /** 叠加草稿后的元素视图：面板与画布都读它，保证「所见即所改」。 */
  const viewElement = useCallback((el: PptElement): PptElement => {
    const d = drafts[`${slideIndex}:${el.id}`];
    if (!d) return el;
    const next: PptElement = { ...el };
    if (Array.isArray(d.text)) next.text = d.text as string[];
    if (typeof d.font_size === "number") {
      next.font_size = (d.font_size as number) * (4 / 3);   // pt → 画布 px
    }
    if (typeof d.fill === "string") next.fill = d.fill;
    if (typeof d.align === "string") {
      next.align = { left: "start", center: "middle", right: "end" }[d.align] ?? el.align;
    }
    if (el.frame) {
      const f: [number, number, number, number] = [...el.frame];
      for (const [i, k] of (["x", "y", "width", "height"] as const).entries()) {
        if (typeof d[k] === "number") f[i] = d[k] as number;
      }
      next.frame = f;
    }
    return next;
  }, [drafts, slideIndex]);

  const currentEl: PptElement | undefined = useMemo(() => {
    const raw = current?.elements.find((e) => e.id === selected);
    return raw ? viewElement(raw) : undefined;
  }, [current, selected, viewElement]);

  // ---------------------------------------------------------------- 数据加载

  const loadOutline = useCallback(async () => {
    const data = await api.pptOutline(project);
    setOutline(data);
    return data;
  }, [project]);

  const loadThumbs = useCallback(async () => {
    try {
      const r = await api.pptThumbs(project);
      const m: Record<number, string> = {};
      for (const t of r.slides) m[t.slide] = rewriteLocalUrls(t.svg);
      setThumbs(m);
    } catch {
      // 缩略图是增强，失败不影响编辑（左栏退回文字标题）
    }
  }, [project]);

  const loadSlide = useCallback(async (slide: number) => {
    const data = await api.pptSlide(project, slide);
    // href 已被后端改写成工作区相对路径，这里转成 /api/files/raw
    setSvg(rewriteLocalUrls(data.svg));
  }, [project]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const data = await loadOutline();
        if (!alive) return;
        const first = data.slides[0]?.slide ?? 1;
        setSlideIndex(first);
        await loadSlide(first);
        void loadThumbs();
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    })();
    return () => { alive = false; };
  }, [loadOutline, loadSlide, loadThumbs]);

  // 画布尺寸：zoom=null 时自适应（可缩小也可放大到 1.5×），
  // 手动缩放下按固定比例渲染——超出容器就由 .ppt-canvas 横向/纵向滚动。
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const compute = () => {
      const rect = el.getBoundingClientRect();
      const vw = current?.viewBox?.[2] ?? 1280;
      const vh = current?.viewBox?.[3] ?? 720;
      const pad = 32;
      const fit = Math.min((rect.width - pad) / vw, (rect.height - pad) / vh);
      const scale = zoom ?? Math.min(fit, 1.5);
      setBox({ w: Math.max(160, vw * scale), h: Math.max(90, vh * scale) });
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [current, zoom]);

  const goSlide = useCallback(async (n: number) => {
    if (!outline) return;
    const target = Math.min(Math.max(1, n), outline.total);
    setSlideIndex(target);
    setSelected(null);
    setBusy(true);
    try {
      await loadSlide(target);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [outline, loadSlide]);

  const flash = (text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(""), 2200);
  };

  /** 属性改动：只并入草稿，不写盘（画布靠 viewElement 即时预览）。 */
  const stageChange = useCallback((changes: Record<string, unknown>) => {
    if (!selected) return;
    const key = `${slideIndex}:${selected}`;
    setDrafts((prev) => ({ ...prev, [key]: { ...(prev[key] ?? {}), ...changes } }));
  }, [selected, slideIndex]);

  /** 提交草稿：一次请求写完全部改动，再刷新一次画布与结构。
   *
   *  frame 类改动会连带影响文字定位，交给后端按当前 anchor 统一重算，
   *  前端只负责把累积值原样送过去。
   */
  const commitDrafts = useCallback(async () => {
    const entries = Object.entries(drafts);
    if (!entries.length) return;
    setBusy(true);
    setError("");
    try {
      let ok = 0;
      const failed: string[] = [];
      // 失败条目必须留在草稿里：否则用户点「提交」后发现部分没落盘，
      // 而那些改动已经从界面消失，等于静默丢失。
      const remaining: Record<string, Record<string, unknown>> = {};
      for (const [key, changes] of entries) {
        const [slideStr, elementId] = key.split(":");
        try {
          await api.pptEdit({
            project, slide: Number(slideStr), element_id: elementId, changes,
          });
          ok += 1;
        } catch (e) {
          remaining[key] = changes;
          failed.push(`第 ${slideStr} 页 ${elementId}：${(e as Error).message}`);
        }
      }
      setDrafts(remaining);
      await Promise.all([loadSlide(slideIndex), loadOutline(), loadThumbs()]);
      const pages = new Set(entries.map(([k]) => k.split(":")[0])).size;
      flash(failed.length
        ? `已提交 ${ok} 处，${failed.length} 处失败（已保留在草稿中）`
        : `已提交 ${ok} 处改动，覆盖 ${pages} 页`);
      if (failed.length) setError(failed.join("；"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [drafts, project, slideIndex, loadSlide, loadOutline, loadThumbs]);

  /** 放弃草稿，回到文件当前状态。 */
  const discardDrafts = useCallback(() => {
    setDrafts({});
    flash("已放弃未提交的改动");
  }, []);

  /** 立即写盘（表格单元格 / 图表数值）：结构型改动无法用覆盖样式预览。 */
  const applyNow = useCallback(async (changes: Record<string, unknown>) => {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.pptEdit({
        project, slide: slideIndex, element_id: selected, changes,
      });
      await Promise.all([loadSlide(slideIndex), loadOutline(), loadThumbs()]);
      flash(`已保存：${r.applied.join("、")}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [selected, project, slideIndex, loadSlide, loadOutline, loadThumbs]);

  const saveAnnotation = useCallback(async () => {
    if (!selected || !annotationDraft.trim()) return;
    setBusy(true);
    try {
      await api.pptAnnotate({
        project, slide: slideIndex, element_id: selected, note: annotationDraft.trim(),
      });
      await loadOutline();
      setAnnotationDraft("");
      flash("标注已保存，点「应用改动」交给 Agent");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [selected, annotationDraft, project, slideIndex, loadOutline]);

  const exportDeck = useCallback(async (format?: "pptx" | "pdf") => {
    setBusy(true);
    setError("");
    try {
      const r = await api.pptExport(
        project, source.replace(/\.pptx$/i, "_edited.pptx"), format);
      if (r.pdf_error) {
        // pptx 已导出成功，PDF 失败单独提示（用户仍有 pptx 可用）
        flash(`pptx 已导出；${r.pdf_error}`);
      } else {
        flash(`已导出 ${r.output}`);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [project, source]);

  // ---------------------------------------------------------------- 页面结构操作（A4）

  const reloadAll = useCallback(async () => {
    const [o] = await Promise.all([loadOutline(), loadThumbs()]);
    if (o && (slideIndex > o.total)) setSlideIndex(Math.max(1, o.total));
    await loadSlide(Math.min(slideIndex, o?.total ?? 1));
    return o;
  }, [loadOutline, loadThumbs, loadSlide, slideIndex]);

  const addSlide = useCallback(async (mode: "blank" | "copy") => {
    setBusy(true);
    setError("");
    try {
      const r = await api.pptAddSlide(mode === "copy"
        ? { project, copy_from: slideIndex }
        : { project, index: slideIndex + 1, layout: "title_content" });
      flash(`已插入第 ${r.slide} 页（共 ${r.slides} 页）`);
      await reloadAll();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [project, slideIndex, reloadAll]);

  const removeSlide = useCallback(async () => {
    if (!outline || outline.total <= 1) { flash("至少保留一页"); return; }
    if (!window.confirm(`确定删除第 ${slideIndex} 页？`)) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.pptRemoveSlide({ project, slide: slideIndex });
      flash(`已删除（余 ${r.slides} 页）`);
      setSlideIndex(Math.max(1, Math.min(slideIndex, r.slides)));
      await reloadAll();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [project, slideIndex, outline, reloadAll]);

  // ---------------------------------------------------------------- 备注（B4）

  // 切页/打开面板时载入该页备注
  useEffect(() => {
    if (!noteOpen) return;
    void (async () => {
      try {
        const r = await api.pptNotes(project);
        const hit = r.notes.find((n) => n.slide === slideIndex);
        setNoteDraft(hit?.note ?? "");
      } catch { setNoteDraft(""); }
    })();
  }, [noteOpen, slideIndex, project]);

  const saveNote = useCallback(async () => {
    setBusy(true);
    try {
      await api.pptSetNote({ project, slide: slideIndex, note: noteDraft.trim() });
      flash("备注已保存（导出 pptx 时写入演讲者备注）");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [project, slideIndex, noteDraft]);

  const applyAnnotations = useCallback(async () => {
    if (busy) return;
    try {
      const r = await api.pptAnnotations(project);
      if (!r.count) {
        // 此前按钮在无标注时是 disabled（不透明度 .45），点了毫无反馈，
        // 用户以为功能坏了。改为始终可点 + 明确指路。
        flash("还没有标注：先点选元素 → 切到「标注」页写一句话 → 保存标注");
        if (selected) setTab("annotate");
        return;
      }
      if (!onRequestApply) {
        flash("此页签未连接对话通道，无法交给 AI");
        return;
      }
      onRequestApply({ project, source, annotations: r.items });
      flash(`已把 ${r.count} 条标注交给 AI，正在切到对话页签…`);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [busy, project, source, onRequestApply, selected]);

  // ---------------------------------------------------------------- 键盘

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA)$/.test(t.tagName)) return;
      if (e.key === "Escape") { setSelected(null); return; }
      if (e.key === "ArrowLeft") { void goSlide(slideIndex - 1); }
      if (e.key === "ArrowRight") { void goSlide(slideIndex + 1); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [goSlide, slideIndex]);

  // ---------------------------------------------------------------- 渲染

  const scale = current?.viewBox ? box.w / current.viewBox[2] : 1;
  /** 当前「适应窗口」对应的比例（缩放控件减号/加号从这里起步）。 */
  const fitScale = current?.viewBox
    ? Math.min(box.w / current.viewBox[2], 1.5)
    : 1;
  const count = outline?.slides.reduce((n, s) => n + s.elements.filter((e) => e.annotation).length, 0) ?? 0;

  /** 草稿态覆盖样式：让未提交的改动也能即时看到效果。
   *
   *  抽成模块级函数：画布（当前页）与缩略图（全页）都要用同一套规则，
   *  两处各写一份必然不一致——缩略图看起来没改、点进去却变了。
   *  同一元素多条规则按「先文字、后位移」顺序拼接，位移用 transform
   *  以免覆盖 x/y 属性（图片与文本的定位语义不同）。
   */
  const draftCss = useMemo(
    () => buildDraftCss(drafts, slideIndex, current?.elements ?? []),
    [drafts, slideIndex, current],
  );

  /** 每一页是否有未提交改动（缩略图角标 + 「跨页提交」提示用）。 */
  const slidesWithDrafts = useMemo(() => {
    const m: Record<number, number> = {};
    for (const key of Object.keys(drafts)) {
      const n = Number(key.split(":")[0]);
      m[n] = (m[n] ?? 0) + 1;
    }
    return m;
  }, [drafts]);

  return (
    <div className="ppt-editor">
      <div className="ppt-editor-head">
        <span className="ppt-editor-title">📊 {source}</span>
        <span className="ppt-editor-meta">
          第 {slideIndex} / {outline?.total ?? "–"} 页
          {count > 0 && <em> · {count} 条标注</em>}
          {draftCount > 0 && (
            <em className="draft">
              {" "}· {draftCount} 处未提交
              {Object.keys(slidesWithDrafts).length > 1
                ? `（跨 ${Object.keys(slidesWithDrafts).length} 页）`
                : ""}
            </em>
          )}
        </span>
        <span className="ppt-editor-actions">
          {busy && <span className="ppt-editor-busy">处理中…</span>}
          {draftCount > 0 && (
            <>
              <button className="btn" onClick={discardDrafts} disabled={busy}>放弃</button>
              <button className="btn primary" onClick={() => void commitDrafts()} disabled={busy}>
                提交改动 ({draftCount})
              </button>
            </>
          )}
          <button className="btn" onClick={() => void addSlide("copy")} disabled={busy}
                  title="复制当前页插入到下一位置（同类内容页最常用）">+ 复制本页</button>
          <button className="btn" onClick={() => void addSlide("blank")} disabled={busy}
                  title="插入一页空白内容页">+ 新页</button>
          <button className="btn" onClick={() => void removeSlide()} disabled={busy}
                  title="删除当前页（至少保留一页）">删本页</button>
          <button className="btn" onClick={() => void exportDeck()} disabled={busy}>导出 pptx</button>
          <button className="btn" onClick={() => void exportDeck("pdf")} disabled={busy}
                  title="需要本机安装 LibreOffice">导出 PDF</button>
          <button className="btn" onClick={() => setNoteOpen((v) => !v)} disabled={busy}
                  title="本页的演讲者备注（导出 pptx 时写入备注栏）">
            备注{noteOpen ? " ▲" : ""}
          </button>
          <button className="btn" onClick={() => void applyAnnotations()} disabled={busy}
            title="把「标注」页写下的修改意图打包成一条消息交给 Agent 执行（需先保存标注）">
            交 AI 应用{count > 0 ? ` (${count})` : ""}
          </button>
        </span>
      </div>

      {error && <div className="ppt-editor-error" onClick={() => setError("")}>{error}</div>}

      {noteOpen && (
        <div className="ppt-note-bar">
          <span className="ppt-note-label">第 {slideIndex} 页备注</span>
          <textarea
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            placeholder="演讲提示：这页要讲的重点、过渡句、时间提醒…"
            rows={2}
          />
          <button className="btn primary" onClick={() => void saveNote()} disabled={busy}>
            保存备注
          </button>
        </div>
      )}

      <div className="ppt-editor-body">
        {/* 左：幻灯片轨 */}
        <div className="ppt-rail">
          {(outline?.slides ?? []).map((s) => {
            const title = s.elements.find((e) => e.placeholder === "title")?.text?.[0]
              ?? s.elements.find((e) => e.text?.length)?.text?.[0] ?? "（空白）";
            const marks = s.elements.filter((e) => e.annotation).length;
            return (
              <button
                key={s.slide}
                className={`ppt-thumb ${s.slide === slideIndex ? "active" : ""}`}
                onClick={() => void goSlide(s.slide)}
                title={title}
              >
                <span className="ppt-thumb-n">{s.slide}</span>
                <span className="ppt-thumb-body">
                  {/* 真实缩略预览：图片已由后端换成占位块，体积很小。
                      草稿按页注入各自的覆盖样式——这样左栏看到的就是
                      「含未提交改动」的真实画面，而不是上次提交后的旧图。 */}
                  {thumbs[s.slide] ? (
                    <ThumbPreview
                      svg={thumbs[s.slide]}
                      slide={s.slide}
                      elements={s.elements}
                      drafts={drafts}
                    />
                  ) : (
                    <span className="ppt-thumb-title">{title}</span>
                  )}
                  <span className="ppt-thumb-tags">
                    {marks > 0 && <span className="ppt-thumb-mark">{marks} 标注</span>}
                    {slidesWithDrafts[s.slide] > 0 && (
                      <span className="ppt-thumb-draft">{slidesWithDrafts[s.slide]} 待提交</span>
                    )}
                  </span>
                </span>
              </button>
            );
          })}
        </div>

        {/* 中：画布。超宽时由 .ppt-canvas 横向滚动（flex-start + margin:auto） */}
        <div className="ppt-canvas" ref={canvasRef} onClick={() => setSelected(null)}>
          <div
            className="ppt-stage"
            style={{ width: box.w, height: box.h }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* 草稿态覆盖样式：让未提交的改动也能即时看到效果 */}
            {draftCss && <style>{draftCss}</style>}
            <div className="ppt-svg" dangerouslySetInnerHTML={{ __html: svg }} />
            {/* 热区：与 SVG 元素一一对应，只负责点选，不改变渲染 */}
            {(current?.elements ?? []).map((raw) => {
              const el = viewElement(raw);          // 用草稿坐标，热区跟着动
              if (!el.frame) return null;
              const [x, y, w, h] = el.frame;
              const dirty = !!drafts[`${slideIndex}:${el.id}`];
              return (
                <button
                  key={el.id}
                  className={`ppt-hotspot ${el.id === selected ? "active" : ""} ${el.annotation ? "annotated" : ""} ${dirty ? "dirty" : ""}`}
                  style={{
                    left: x * scale, top: y * scale,
                    width: Math.max(6, w * scale), height: Math.max(6, h * scale),
                  }}
                  onClick={(e) => { e.stopPropagation(); setSelected(el.id); setTab("props"); }}
                  title={`${el.kind}${el.placeholder ? ` · ${el.placeholder}` : ""}${dirty ? " · 未提交" : ""}`}
                />
              );
            })}
          </div>

          {/* 缩放控件：超过 100% 时容器出现横向滚动条 */}
          <div className="ppt-zoom" onClick={(e) => e.stopPropagation()}>
            <button onClick={() => setZoom((z) => Math.max(0.25, Math.round(((z ?? fitScale) - 0.1) * 100) / 100))}
                    title="缩小">−</button>
            <button className={zoom === null ? "on" : ""} onClick={() => setZoom(null)} title="适应窗口">
              {zoom === null ? "适应" : `${Math.round((zoom) * 100)}%`}
            </button>
            <button onClick={() => setZoom(1)} className={zoom === 1 ? "on" : ""} title="100%">1:1</button>
            <button onClick={() => setZoom((z) => Math.min(3, Math.round(((z ?? fitScale) + 0.1) * 100) / 100))}
                    title="放大">+</button>
          </div>
        </div>

        {/* 右：检查器（只选中时出现，浏览态让出宽度） */}
        {selected && currentEl && (
          <div className="ppt-inspector">
            <div className="ppt-inspector-head">
              <span>{kindLabel(currentEl)}</span>
              <code>{currentEl.id}</code>
            </div>
            <div className="ppt-inspector-tabs">
              <button className={tab === "props" ? "on" : ""} onClick={() => setTab("props")}>属性</button>
              <button className={tab === "annotate" ? "on" : ""} onClick={() => setTab("annotate")}>
                标注{currentEl.annotation ? " ●" : ""}
              </button>
            </div>
            <div className="ppt-inspector-body">
              {tab === "props" ? (
                <PropsPanel el={currentEl} busy={busy} onStage={stageChange} onApply={applyNow} />
              ) : (
                <div className="ppt-annotate">
                  <div className="ppt-hint">描述你要怎么改，交给 Agent 判断执行。</div>
                  <textarea
                    value={annotationDraft || currentEl.annotation || ""}
                    onChange={(e) => setAnnotationDraft(e.target.value)}
                    placeholder="例：这行太挤，精简成 3 个要点"
                    rows={4}
                  />
                  <div className="ppt-row">
                    <button className="btn primary" onClick={() => void saveAnnotation()}
                            disabled={busy || !annotationDraft.trim()}>保存标注</button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {toast && <div className="ppt-toast">{toast}</div>}
    </div>
  );
}

function kindLabel(el: PptElement): string {
  if (el.table) return "表格";
  if (el.chart) return "图表";
  if (el.image) return "图片";
  if (el.kind === "graphic-frame") return "原生对象";
  return "文本";
}

/** 属性面板：按元素类型分化。所有改动都是「确定性」的——直接写 SVG，不经模型。
 *
 *  两级提交：
 *  - onStage：只进内存草稿，画布即时预览（文本/字号/颜色/对齐/位置）；
 *  - onApply：立即写盘（表格单元格、图表数值——它们是 JSON 结构，
 *    无法用覆盖样式预览，改完直接落盘更符合预期）。 */
function PropsPanel({ el, busy, onStage, onApply }: {
  el: PptElement;
  busy: boolean;
  onStage: (changes: Record<string, unknown>) => void;
  onApply: (changes: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState((el.text ?? []).join("\n"));
  const [pt, setPt] = useState<string>(String(pxToPt(el.font_size) ?? ""));
  const [fill, setFill] = useState(el.fill ?? "#000000");
  const [frame, setFrame] = useState<[number, number, number, number]>(
    el.frame ?? [0, 0, 0, 0],
  );
  const [textDirty, setTextDirty] = useState(false);

  // 只在「切换选中元素」时重置本地输入，不能依赖 el 本身：
  // el 是叠加草稿后的新对象，任何一次 onStage（如点对齐）都会让它换引用，
  // 若据此重置，用户正在输入框里敲的文字会被草稿值冲掉。
  const elKeyRef = useRef(el.id);
  useEffect(() => {
    if (elKeyRef.current === el.id) return;
    elKeyRef.current = el.id;
    setText((el.text ?? []).join("\n"));
    setPt(String(pxToPt(el.font_size) ?? ""));
    setFill(el.fill ?? "#000000");
    setFrame(el.frame ?? [0, 0, 0, 0]);
    setTextDirty(false);
  }, [el]);

  // ---- 文本
  if (el.text?.length) {
    return (
      <div className="ppt-fields">
        <label>内容</label>
        <textarea rows={Math.min(6, Math.max(2, el.text.length))} value={text}
                  onChange={(e) => { setText(e.target.value); setTextDirty(true); }} />
        <div className="ppt-grid2">
          <label>字号 (pt)</label>
          <label>颜色</label>
          <input value={pt} onChange={(e) => setPt(e.target.value)} />
          <input type="color" value={normalizeColor(fill)} onChange={(e) => setFill(e.target.value)} />
        </div>
        <label>对齐</label>
        <div className="ppt-btnrow">
          {(["left", "center", "right"] as const).map((a) => (
            <button key={a}
                    className={alignOf(el.align) === a ? "on" : ""}
                    onClick={() => onStage({ align: a })}>
              {{ left: "左", center: "中", right: "右" }[a]}
            </button>
          ))}
        </div>
        <FrameFields frame={frame} setFrame={setFrame} />
        <div className="ppt-row">
          <button className="btn primary" disabled={busy}
                  onClick={() => {
                    onStage({
                      ...(textDirty ? { text: text.split("\n") } : {}),
                      ...(pt ? { font_size: Number(pt) } : {}),
                      ...(fill ? { fill: normalizeColor(fill) } : {}),
                      x: frame[0], y: frame[1], width: frame[2], height: frame[3],
                    });
                    setTextDirty(false);
                  }}>
            加入待提交
          </button>
        </div>
        <div className="ppt-hint">改动先进草稿、画布即时显示，点顶部「提交改动」才写盘。</div>
      </div>
    );
  }

  // ---- 表格：只改单元格，不增删行列（结构改动走标注）
  if (el.table) {
    const rows = el.table.rows ?? [];
    return (
      <div className="ppt-fields">
        <div className="ppt-hint">点击单元格直接改文字。增删行列请用「标注」。</div>
        <table className="ppt-mini">
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri}>
                {row.map((cell, ci) => (
                  <td key={ci}>
                    <input
                      defaultValue={cell.text}
                      onBlur={(e) => {
                        if (e.target.value !== cell.text) {
                          onApply({ table_cell: { row: ri, col: ci, text: e.target.value } });
                        }
                      }}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="ppt-hint">
          {rows.length} 行 × {rows[0]?.length ?? 0} 列 · 导出为原生表格
        </div>
      </div>
    );
  }

  // ---- 图表：只改数值，不增删系列
  if (el.chart) {
    const cats = el.chart.categories ?? [];
    const values = el.chart.series?.[0]?.values ?? [];
    return (
      <div className="ppt-fields">
        <div className="ppt-hint">改数值即可，导出为原生图表（非图片）。增删系列请用「标注」。</div>
        <table className="ppt-mini">
          <tbody>
            {cats.map((c, i) => (
              <tr key={i}>
                <td className="ppt-mini-cat">{c}</td>
                <td>
                  <input
                    defaultValue={String(values[i] ?? "")}
                    onBlur={(e) => {
                      const v = Number(e.target.value);
                      if (!Number.isNaN(v) && v !== values[i]) {
                        onApply({ chart_value: { series: 0, index: i, value: v } });
                      }
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="ppt-hint">{el.chart.type ?? "chart"} · {el.chart.series?.length ?? 0} 个系列</div>
      </div>
    );
  }

  // ---- 图片：换图保留原位置尺寸
  if (el.image) {
    return (
      <div className="ppt-fields">
        <label>当前文件</label>
        <code className="ppt-file">{el.image.split("/").pop()}</code>
        <label>替换图片（工作区相对路径）</label>
        <input placeholder="素材/新图.png" onKeyDown={(e) => {
          if (e.key === "Enter") {
            const v = (e.target as HTMLInputElement).value.trim();
            if (v) onApply({ image: v });
          }
        }} />
        <div className="ppt-hint">回车确认。换图保留原有位置与尺寸。</div>
        <FrameFields frame={frame} setFrame={setFrame} />
        <div className="ppt-row">
          <button className="btn" disabled={busy}
                  onClick={() => onStage({ x: frame[0], y: frame[1], width: frame[2], height: frame[3] })}>
            加入待提交
          </button>
        </div>
      </div>
    );
  }

  return <div className="ppt-hint">该元素暂无可编辑属性。</div>;
}

function FrameFields({ frame, setFrame }: {
  frame: [number, number, number, number];
  setFrame: (f: [number, number, number, number]) => void;
}) {
  const set = (i: number, v: string) => {
    const n = Number(v);
    if (Number.isNaN(n)) return;
    const next = [...frame] as [number, number, number, number];
    next[i] = n;
    setFrame(next);
  };
  return (
    <div className="ppt-grid2">
      <label>X</label><label>Y</label>
      <input defaultValue={Math.round(frame[0])} onBlur={(e) => set(0, e.target.value)} />
      <input defaultValue={Math.round(frame[1])} onBlur={(e) => set(1, e.target.value)} />
      <label>宽</label><label>高</label>
      <input defaultValue={Math.round(frame[2])} onBlur={(e) => set(2, e.target.value)} />
      <input defaultValue={Math.round(frame[3])} onBlur={(e) => set(3, e.target.value)} />
    </div>
  );
}



const alignOf = (a: string | null | undefined): "left" | "center" | "right" => {
  if (a === "middle") return "center";
  if (a === "end") return "right";
  return "left";
};

/** 给一段 SVG 里所有 id 加作用域前缀，返回 [新SVG, 旧id→新id]。
 *
 *  为什么必须做：左栏会同时内联 N 页缩略 SVG，而每页的元素 id 都从
 *  shape-2 开始——不加前缀就是文档级 id 重复。CSS 的 `#shape-2` 只命中文档
 *  里第一个匹配，结果「改第 3 页的标题，第 1 页缩略图跟着变」。
 *  用 DOMParser 处理（而非正则替换），同时重写 url(#id) 引用，
 *  否则渐变/剪裁路径会串到别的页。 */
function scopeSvgIds(svg: string, prefix: string): [string, Map<string, string>] {
  const map = new Map<string, string>();
  let out = svg;
  try {
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    const root = doc.documentElement;
    if (!root || root.nodeName === "parsererror") return [svg, map];
    root.querySelectorAll("[id]").forEach((el) => {
      const old = el.getAttribute("id")!;
      const next = `${prefix}${old}`;
      map.set(old, next);
      el.setAttribute("id", next);
    });
    // 重写 url(#old) / href="#old" 引用
    root.querySelectorAll("*").forEach((el) => {
      for (const attr of Array.from(el.attributes)) {
        if (!attr.value.includes("#")) continue;
        let v = attr.value;
        for (const [old, next] of map) {
          v = v.split(`url(#${old})`).join(`url(#${next})`);
          if (v === `#${old}`) v = `#${next}`;
        }
        if (v !== attr.value) el.setAttribute(attr.name, v);
      }
    });
    out = new XMLSerializer().serializeToString(root);
  } catch {
    return [svg, map];
  }
  return [out, map];
}

/** 左栏缩略图：内联 SVG + 本页草稿覆盖样式。
 *
 *  每个缩略图的 id 都加 `t{页码}-` 前缀：多页同时内联时原始 id 会重复，
 *  而 CSS 的 `#shape-2` 只命中文档里第一个匹配——不隔离就会出现
 *  「改第 3 页，第 1 页缩略图跟着变」。
 */
function ThumbPreview({ svg, slide, elements, drafts }: {
  svg: string;
  slide: number;
  elements: PptElement[];
  drafts: Record<string, Record<string, unknown>>;
}) {
  const [scoped, idMap] = useMemo(() => scopeSvgIds(svg, `t${slide}-`), [svg, slide]);
  const css = useMemo(
    () => buildDraftCss(drafts, slide, elements, idMap),
    [drafts, slide, elements, idMap],
  );
  return (
    <span className="ppt-thumb-preview">
      <style>{css}</style>
      <span dangerouslySetInnerHTML={{ __html: scoped }} />
    </span>
  );
}

/** 推算文本当前锚点 x（由元素 frame 与 svg text-anchor 反推）。
 *
 *  为什么不能直接读 SVG 里的 x：草稿层只有元素元数据（frame/align），
 *  拿不到内联 SVG 的实时属性；而 ppt-master 生成的锚点规则是确定的
 *  （left→frame.x+pad、center→中线、right→frame.x+fw-pad），据此反推即可。
 *  这与后端 _apply_text_style 的落盘规则保持一致。 */
function readTextX(el: PptElement): number {
  const [fx, , fw] = el.frame ?? [0, 0, 0, 0];
  const pad = 9.6;
  const a = el.align;
  if (a === "middle" || a === "center") return fx + fw / 2;
  if (a === "end" || a === "right") return fx + fw - pad;
  return fx + pad;
}

/** 草稿态覆盖样式生成（画布与缩略图共用）。
 *
 *  SVG 是死文本，React 改不了里面节点的样式，只能靠一段覆盖 CSS 表达
 *  「还没写盘但用户已经改了」的效果：
 *  - 字号/颜色/对齐改 `<元素id> text`；
 *  - 位移改元素自身 transform（不能改 x/y 属性：文本的 x 是锚点、
 *    图片的 x 是左上角，语义不同，统一用 transform 才安全）。
 *  提交后草稿清空，覆盖样式随之消失，画面由后端真实写盘结果接管。 */
export function buildDraftCss(
  drafts: Record<string, Record<string, unknown>>,
  slideIndex: number,
  elements: PptElement[],
  idMap?: Map<string, string>,
): string {
  const byId = new Map(elements.map((e) => [e.id, e]));
  const rules: string[] = [];
  for (const [key, ch] of Object.entries(drafts)) {
    const [sl, eid] = key.split(":");
    if (Number(sl) !== slideIndex) continue;
    const raw = byId.get(eid);
    if (!raw) continue;
    // 缩略图场景要换成加了作用域前缀的 id，否则选择器命中的是别的页
    const id = `#${CSS.escape(idMap?.get(eid) ?? eid)}`;
    if (typeof ch.font_size === "number") {
      rules.push(`${id} text{font-size:${(ch.font_size as number) * 4 / 3}px !important}`);
    }
    if (typeof ch.fill === "string") {
      rules.push(`${id} text{fill:${ch.fill} !important}`);
    }
    if (typeof ch.align === "string") {
      // 注意：x 不是 CSS 属性，`text{x:300px}` 会被浏览器静默忽略，
      // 结果 anchor 变了、锚点却还停在原位——左对齐的文字从中线往右排，
      // 右对齐的文字结束在中线，看起来就是「左右反了」。必须用变换实现位移。
      const anchor = { left: "start", center: "middle", right: "end" }[ch.align as string];
      rules.push(`${id} text{text-anchor:${anchor} !important}`);
      if (raw.frame) {
        const [fx, , fw] = raw.frame;
        const pad = 9.6;
        const target = ch.align === "left" ? fx + pad
          : ch.align === "right" ? fx + fw - pad : fx + fw / 2;
        const base = readTextX(raw);
        const dx = target - base;
        if (Math.abs(dx) > 0.01) {
          rules.push(`${id} text{transform:translateX(${dx}px)}`);
        }
      }
    }
    if (raw.frame) {
      const [fx, fy, fw, fh] = raw.frame;
      const nx = typeof ch.x === "number" ? (ch.x as number) : fx;
      const ny = typeof ch.y === "number" ? (ch.y as number) : fy;
      const nw = typeof ch.width === "number" ? (ch.width as number) : fw;
      const nh = typeof ch.height === "number" ? (ch.height as number) : fh;
      const parts: string[] = [];
      if (nx !== fx || ny !== fy) parts.push(`translate(${nx - fx}px,${ny - fy}px)`);
      if (nw !== fw || nh !== fh) {
        // 以元素左上角为原点缩放，避免放大后整体漂移
        parts.push(`scale(${fw ? nw / fw : 1},${fh ? nh / fh : 1})`);
      }
      if (parts.length) {
        rules.push(`${id}{transform:${parts.join(" ")};transform-origin:${fx}px ${fy}px}`);
      }
    }
    if (Array.isArray(ch.text)) {
      // 文字内容无法用 CSS 改，画布上高亮标出「该元素有未提交的文字改动」
      rules.push(`${id} text{text-decoration:underline dashed rgba(79,140,255,.6)}`);
    }
  }
  return rules.join("\n");
}

/** <input type="color"> 只认 #rrggbb；SVG 里可能是 #fff / 颜色名 / none。 */
function normalizeColor(c: string): string {
  if (/^#[0-9a-f]{6}$/i.test(c)) return c;
  if (/^#[0-9a-f]{3}$/i.test(c)) {
    return "#" + c.slice(1).split("").map((x) => x + x).join("");
  }
  return "#000000";
}

export { ptToPx };
