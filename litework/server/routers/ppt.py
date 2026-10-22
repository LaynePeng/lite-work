# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""PPT 编辑器：把 ppt_edit 工具的能力暴露给「PPT 编辑器」页签。

为什么需要独立路由：ppt_edit 是 Agent 工具，而编辑器页签是「人在界面上
直接改」的通道——点选元素、改属性这类确定性操作不该绕一圈让模型转述。
两者共用同一份 PptEditTools 实现，保证界面改动与 Agent 改动落盘结果一致。

与 /api/files/* 的分工：本路由只管 roundtrip 工作区（导入 / 读页 / 改元素
/ 标注 / 导出），文件浏览与预览仍在 office 路由。
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .context import ServerContext


class PptUnavailable(RuntimeError):
    """ppt-master 技能不可用（未随包分发 / 资产缺失 / 被删）。

    单独成类型（而不是混进 PptEditError）：路由层要把它映射成 **409 + 可执行
    修复建议**，而不是 422「参数不对」——用户看得到的是「能力没装好」，
    而不是「你选错了套件」。
    """

    def __init__(self, info: Dict[str, Any]) -> None:
        self.info = info
        super().__init__(info.get("reason") or "PPT 模板能力不可用")


class OpenBody(BaseModel):
    path: str = Field(..., description="源 pptx 路径（相对工作区）")
    reimport: bool = Field(False, description="已有工作区时是否丢弃改动重新导入")


class SlideQuery(BaseModel):
    project: str
    slide: int


class EditBody(BaseModel):
    project: str
    slide: int
    element_id: str
    changes: Dict[str, Any]


class AnnotateBody(BaseModel):
    project: str
    slide: int
    element_id: str
    note: str


class ExtractBody(BaseModel):
    path: str = Field(..., description="源 pptx 路径（相对工作区）")


class ExportBody(BaseModel):
    project: str
    output: str | None = None
    format: str | None = Field(
        None, description="导出格式：pptx（默认）/ pdf（需本机 LibreOffice）")


class CreateFromTemplateBody(BaseModel):
    template: Dict[str, Any] = Field(..., description="{suite, brand, style, pages:[{page, values}]}")
    filename: str | None = None
    images: Dict[str, str] | None = Field(
        None, description="需生成的配图：{'3:IMAGE_1': 'english visual prompt'}"
        "（键为「页码:图片位」，值交给生图模型）")
    image_backend: str | None = Field(
        None, description="生图后端（留空用设置里的默认；/api/ppt/images 可查可用项）")
    image_mode: str | None = Field(
        None, description="配图方式：gen=生图模型 / stock=开放图库。"
        "留空由后端按能力自动选；生图失败会降级到图库")


class PlanBody(BaseModel):
    """提示词/源内容 → 逐页版式 + 文案（不直接生成文件）。"""
    suite: str = Field(..., description="版式套件 key（/api/ppt/templates 返回）")
    brief: str = Field("", description="需求描述；有 source_path 时可留空")
    audience: str | None = Field(None, description="受众/场合，可选")
    page_hint: int = Field(0, ge=0, le=30, description="期望页数，0 = 交给模型")
    source_path: str | None = Field(None, description="已有 pptx：重排为规划输入")
    fallback: bool = Field(
        True, description="模型不可用时是否返回兜底骨架页（false 则直接报错）")
    with_images: bool = Field(
        False, description="是否让模型为带图版式写配图提示词（结果里带可用生图后端）")
    rewrite: dict | None = Field(
        None, description="单页重写模式：{page, values, instruction}"
        "——只改这一页文案，不重排其他页")


def create_router(ctx: ServerContext) -> APIRouter:
    router = APIRouter()

    def _tools(request: Request):
        from ...tools.ppt_edit import PptEditTools

        workspace = ctx.require_workspace(request)
        return PptEditTools(workspace)

    def _require_ppt_skill(request: Request) -> None:
        """模板填充前先确认 ppt-master 可用，缺失时 409 + 修复建议。"""
        from ...tools import ppt_templates

        avail = ppt_templates.availability()
        if not avail["ready"]:
            raise HTTPException(status_code=409, detail=avail.get("hint") or avail.get("reason"))

    async def _call(request: Request, **kwargs) -> Any:
        """把工具返回的 JSON 文本转成对象；[PPT Error] 转 HTTP 422。

        工具层刻意返回文本（给模型读），路由层要结构化数据给前端，
        这里做一次收口，避免两边各自解析。
        """
        import json

        tools = _tools(request)
        raw = await tools.execute("ppt_edit", kwargs)
        if raw.startswith("[PPT Error]"):
            raise HTTPException(status_code=422, detail=raw.split("]:", 1)[-1].strip())
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": True, "result": raw}

    @router.post("/api/ppt/open")
    async def ppt_open(body: OpenBody, request: Request):
        """导入 pptx，建立 roundtrip 工作区。

        需要 ppt-master 的 pptx_to_svg.py——技能缺失时给 409 + 修复建议，
        而不是让用户对着「[PPT Error] 找不到内置技能」的原始文本猜。
        """
        ctx.check_auth(request)
        _require_ppt_skill(request)
        return await _call(request, op="open", path=body.path,
                           reimport=body.reimport)

    @router.get("/api/ppt/outline")
    async def ppt_outline(project: str, request: Request, slide: int | None = None):
        """页结构：左栏缩略图轨、中区画布、右栏面板的共同数据源。"""
        ctx.check_auth(request)
        kwargs: Dict[str, Any] = {"op": "outline", "project": project}
        if slide is not None:
            kwargs["slide"] = slide
        return await _call(request, **kwargs)

    @router.get("/api/ppt/slide")
    async def ppt_slide(project: str, slide: int, request: Request):
        """单页 SVG（href 已改写为工作区相对路径）。"""
        ctx.check_auth(request)
        return await _call(request, op="slide_svg", project=project, slide=slide)

    @router.get("/api/ppt/thumbs")
    async def ppt_thumbs(project: str, request: Request):
        """每页缩略 SVG（左栏预览）。图片已替换为占位块以减小体积。"""
        ctx.check_auth(request)
        return await _call(request, op="thumbs", project=project)

    @router.post("/api/ppt/edit")
    async def ppt_edit(body: EditBody, request: Request):
        """确定性改动：文字 / 字号 / 颜色 / 位置 / 表格单元格 / 图表数值 / 图片。"""
        ctx.check_auth(request)
        return await _call(request, op="edit", project=body.project,
                           slide=body.slide, element_id=body.element_id,
                           changes=body.changes)

    @router.post("/api/ppt/annotate")
    async def ppt_annotate(body: AnnotateBody, request: Request):
        """写自然语言标注（交给 Agent 判断执行）。"""
        ctx.check_auth(request)
        return await _call(request, op="annotate", project=body.project,
                           slide=body.slide, element_id=body.element_id,
                           note=body.note)

    @router.get("/api/ppt/annotations")
    async def ppt_annotations(project: str, request: Request):
        """待处理的标注清单（「应用改动」时回传给 Agent 的上下文）。"""
        ctx.check_auth(request)
        return await _call(request, op="annotations", project=project)

    @router.post("/api/ppt/export")
    async def ppt_export(body: ExportBody, request: Request):
        """回写为 pptx（format=pdf 时再转 PDF）。未改页字节级 passthrough。"""
        ctx.check_auth(request)
        kwargs: Dict[str, Any] = {"op": "export", "project": body.project}
        if body.output:
            kwargs["output"] = body.output
        if body.format:
            kwargs["format"] = body.format
        return await _call(request, **kwargs)

    @router.get("/api/ppt/notes")
    async def ppt_notes(project: str, request: Request):
        """读全部页的演讲者备注。"""
        ctx.check_auth(request)
        return await _call(request, op="get_notes", project=project)

    @router.post("/api/ppt/set_note")
    async def ppt_set_note(request: Request, body: Dict[str, Any]):
        """写单页演讲者备注（note 为空 = 清除）。"""
        ctx.check_auth(request)
        return await _call(request, op="set_note", project=body.get("project"),
                           slide=body.get("slide"), note=body.get("note") or "")

    @router.post("/api/ppt/slide-ops")
    async def ppt_slide_ops(request: Request, body: Dict[str, Any]):
        """页面结构操作：增页（add）/ 删页（remove）/ 换版式（change_layout）。

        统一入口而不是三个端点：三者共享 project 校验，前端也只用一个 api 函数。
        结构变更后工作区自动降级 flat 导出（roundtrip 校验必然失配）。
        """
        ctx.check_auth(request)
        op = str(body.get("op") or "").strip()
        op_map = {"add": "add_slide", "remove": "remove_slide",
                  "change_layout": "change_layout"}
        real_op = op_map.get(op)
        if not real_op:
            raise HTTPException(status_code=422,
                                detail=f"op 仅支持 add/remove/change_layout（当前: {op}）")
        kwargs: Dict[str, Any] = {"op": real_op,
                                  "project": str(body.get("project") or "")}
        for key in ("slide", "index", "layout", "suite", "copy_from"):
            if body.get(key) is not None:
                kwargs[key] = body.get(key)
        if isinstance(body.get("values"), dict):
            kwargs["values"] = body["values"]
        return await _call(request, **kwargs)

    @router.post("/api/ppt/extract")
    async def ppt_extract(body: ExtractBody, request: Request):
        """读已有 pptx 的逐页结构化内容（重排到模板的第一步）。"""
        ctx.check_auth(request)
        return await _call(request, op="extract", path=body.path)

    @router.get("/api/ppt/templates")
    async def ppt_templates(request: Request):
        """可用版式套件 / 品牌配色 / 设计风格。

        响应里带 `available`：技能缺失时 `layouts` 是空数组，但前端要能
        区分「模板库为空」和「ppt-master 没装好」——后者必须提示修复安装，
        否则用户看到的就是一个永远空的模板页。
        """
        ctx.check_auth(request)
        return await _call(request, op="templates")

    @router.get("/api/ppt/images")
    async def ppt_images(request: Request):
        """配图能力：生图 / 图库 / 都没有。

        不能只报「生图后端」——没配生图模型的用户会以为配图不可用，而其实
        开放图库（Openverse / Wikimedia）零配置就能用。
        """
        from ...tools import ppt_images

        ctx.check_auth(request)
        providers = _llm_providers_for_images(ctx)
        cfg = (ctx.app.config.get("ppt_images") or {})
        cap = ppt_images.capability(providers, cfg)
        return {
            **cap,
            # 兼容旧字段（前端早期只用 backends）
            "backends": cap["backends"],
            "enabled": bool(cfg.get("enabled", False)),
        }

    @router.post("/api/ppt/create")
    async def ppt_create(body: CreateFromTemplateBody, request: Request):
        """按模板填充生成 pptx，并立刻转入编辑器工作区。

        带 `images` 时先跑生图再填进图片位：生图是花额度、十几秒起步的操作，
        失败不能把整份稿子废掉——单张失败只记 warning，其余照常出稿。
        """
        ctx.check_auth(request)
        _require_ppt_skill(request)
        workspace = ctx.require_workspace(request)

        warnings: List[str] = []
        template = dict(body.template)
        if body.images:
            filled = await _generate_images(
                ctx, request, workspace, template, body.images,
                body.image_backend or "", warnings, body.image_mode or "")
            template = filled

        kwargs: Dict[str, Any] = {"op": "create_from_template", "template": template}
        if body.filename:
            kwargs["filename"] = body.filename
        result = await _call(request, **kwargs)
        if warnings and isinstance(result, dict):
            result = {**result, "warnings": (result.get("warnings") or []) + warnings}
        return result

    @router.post("/api/ppt/preview")
    async def ppt_preview(request: Request, body: Dict[str, Any]):
        """渲染一页的预览 SVG（规划结果所见即所得）。

        输入：{suite, page, values, table?, chart?}——与 create 同构但只出
        SVG 不落盘。规划阶段的「这页长什么样」全靠它：文字列表看不出
        版式效果，用户等于盲选。
        """
        from ...tools import ppt_templates as tpl

        ctx.check_auth(request)
        suite = str(body.get("suite") or "").strip()
        slug = str(body.get("page") or "").strip()
        if not suite or not slug:
            raise HTTPException(status_code=422, detail="缺少 suite / page")
        try:
            svg, _ = tpl.prepare_page_svg(suite, tpl.resolve_page_file(suite, slug))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        values = body.get("values") or {}
        if not isinstance(values, dict):
            values = {}
        filled, overflow = tpl.fill_page_svg_checked(
            svg, {k: str(v) for k, v in values.items()})
        if body.get("table") is not None:
            filled = tpl.inject_table(filled, body["table"])
        if body.get("chart") is not None:
            filled = tpl.inject_chart(filled, body["chart"])
        return {"ok": True, "svg": filled,
                "overflow": overflow}

    @router.post("/api/ppt/plan")
    async def ppt_plan(body: PlanBody, request: Request):
        """提示词/旧稿 → 逐页方案（版式 + 占位符文案），不落盘。

        为什么放在后端而不是前端直连模型：模板清单、占位符含义、版式合法性校验
        都在 tools/ppt_templates 与 tools/ppt_plan 里，前端不该复制一份；
        而且这一步与 create 用的是同一套 page 标识，规划结果可以原样喂回去。
        """
        from ...tools import ppt_plan

        ctx.check_auth(request)
        workspace = ctx.require_workspace(request)
        try:
            suite_info = ppt_plan_info(body.suite)
        except PptUnavailable as exc:
            raise HTTPException(status_code=409, detail=exc.info.get("hint") or str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        if body.rewrite:
            # 单页重写：小范围 LLM 调用，只改这一页的占位符文案。
            rw = body.rewrite
            slug = str(rw.get("page") or "").strip()
            current = rw.get("values") or {}
            instruction = str(rw.get("instruction") or "").strip()
            if not slug or not instruction:
                raise HTTPException(status_code=422, detail="rewrite 需要 page 与 instruction")
            meta = next((p for p in suite_info["pages"] if p["page"] == slug), None)
            if meta is None:
                raise HTTPException(status_code=422, detail=f"未知版式：{slug}")
            messages = ppt_plan.rewrite_prompt(
                page_label=meta["label"],
                placeholders={p: ppt_plan.PLACEHOLDER_HINTS.get(p, p)
                              for p in meta["placeholders"]},
                current={k: str(v) for k, v in current.items()
                         if isinstance(current, dict)},
                instruction=instruction)
            raw, error = await _run_plan_llm(ctx, messages)
            if raw:
                try:
                    values = ppt_plan.parse_rewrite(raw, set(meta["placeholders"]))
                    return {"ok": True, "rewrite": True, "values": values,
                            "error": None}
                except ppt_plan.PptPlanError as exc:
                    error = str(exc)
            raise HTTPException(status_code=422, detail=error or "重写失败")

        if body.source_path and not body.brief.strip():
            brief = f"把「{body.source_path}」重排到这套版式"
        else:
            brief = body.brief.strip()
        if not brief and not body.source_path:
            raise HTTPException(status_code=422, detail="请填写需求描述")

        source_slides = None
        if body.source_path:
            source_slides = await _extract_slides(workspace, body.source_path)
            if not source_slides:
                raise HTTPException(status_code=422, detail="没能读到源文件内容")

        messages = ppt_plan.build_prompt(
            suite_info=suite_info, brief=brief, audience=body.audience or "",
            page_hint=body.page_hint, source_slides=source_slides,
            with_images=body.with_images,
        )
        raw, error = await _run_plan_llm(ctx, messages)
        image_info = None
        if body.with_images:
            from ...tools import ppt_images

            providers = _llm_providers_for_images(ctx)
            cfg = (ctx.app.config.get("ppt_images") or {})
            backends = ppt_images.available_backends(providers, cfg)
            image_info = {
                "available": bool(backends),
                "backends": backends,
                "default_backend": str(cfg.get("backend") or ""),
            }
        if raw:
            try:
                pages = ppt_plan.parse_plan(raw, suite_info)
                return {"ok": True, "pages": pages, "fallback": False, "error": None,
                        "images": image_info}
            except ppt_plan.PptPlanError as exc:
                error = str(exc)
        if not body.fallback:
            raise HTTPException(status_code=422, detail=error or "规划失败")
        pages = ppt_plan.fallback_plan(suite_info, brief, body.page_hint)
        return {"ok": True, "pages": pages, "fallback": True, "error": error,
                "images": image_info}

    return router


def _stock_query(prompt: str) -> str:
    """把英文画面描述压成图库能检到的短检索词。

    图库是全文检索，长句命中率极低（实测长描述常常 0 结果）；取前几个
    实词（去掉 no text / watermark 这类给生图模型的约束）更靠谱。
    """
    text = re.sub(r"(?i)\b(no text|no letters|no watermark|high quality|well-lit|"
                  r"clean professional presentation visual|suitable as a slide illustration)\b",
                  " ", prompt or "")
    words = [w for w in re.split(r"[,\s]+", text) if len(w) > 2]
    return " ".join(words[:4]) or (prompt or "")[:60]


def _llm_providers_for_images(ctx: ServerContext) -> Dict[str, Dict[str, Any]]:
    """取供应商配置的**真实** api_key，供生图后端使用。

    不能用 `ctx.app.get_llm_config()`：它走 `to_config(persist_key=False)`，
    api_key 被刻意清空（防前端泄露）——用它做凭据映射等于永远拿不到 key，
    表现为「配了 OpenAI 却说没有生图凭据」。这里直接读 registry 内存态。
    """
    try:
        raw = ctx.app.llm_registry.providers
    except AttributeError:
        return {}
    return {
        pid: {"api_key": str(conf.get("api_key") or ""),
              "base_url": str(conf.get("base_url") or ""),
              "model": str(conf.get("model") or "")}
        for pid, conf in raw.items()
    }


async def _generate_images(
    ctx: ServerContext,
    request: Request,
    workspace: str,
    template: Dict[str, Any],
    images: Dict[str, str],
    backend: str,
    warnings: List[str],
    mode: str = "",
) -> Dict[str, Any]:
    """为 `"页码:图片位"` → 提示词 配图，把结果写进对应页的 values。

    两条通道，按能力自动选（`mode` 可强制）：

    - **gen**：调生图大模型按需画。最贴合内容，但需要已配置的供应商密钥；
    - **stock**：检索开放图库（Openverse/Wikimedia，**零密钥**）。没有生图模型
      时这就是默认路径，不是「功能不可用」。

    gen 失败会**逐张降级**到 stock：宁可给一张相关实拍图，也不要留个空洞。
    单张失败只记 warning——配图是增强项，不能因为它让整份稿子出不来。
    """
    from ...tools import ppt_images

    providers = _llm_providers_for_images(ctx)
    cfg = (ctx.app.config.get("ppt_images") or {})
    env = ppt_images._skill_env(providers, ctx.app.config)
    use_backend = backend or str(cfg.get("backend") or "")
    cap = ppt_images.capability(providers, cfg)

    # 兼容 "gen"/"generate" 两种写法：capability() 返回 "generate"，
    # 而 API 参数文档写的是 "gen"——两者必须都能识别（踩过：只认 "gen"
    # 会让 "generate" 落到 stock 分支，生图永远不被调用）
    use_mode = (mode or "").strip().lower()
    if use_mode == "gen":
        use_mode = "generate"
    if use_mode not in ("generate", "stock"):
        use_mode = cap["mode"]
    if use_mode == "generate" and not cap["can_generate"]:
        # 指定了生图但没凭据：降级图库，并说明原因（而不是静默跳过配图）
        warnings.append("没有可用的生图凭据，已改用开放图库配图"
                        "（想要 AI 生图请在设置里配置 OpenAI / 通义万相 / 智谱）")
        use_mode = "stock" if cap["can_stock"] else "none"
    if use_mode == "none":                    # pragma: no cover - openverse 恒可用
        warnings.append("配图不可用：既没有生图凭据，也没有可用图库——已跳过配图")
        return template

    pages = template.get("pages")
    if not isinstance(pages, list):
        return template
    out_dir = os.path.join(workspace, "素材", "ppt-images")

    jobs: List[Tuple[int, str, str]] = []
    for key, prompt in images.items():
        try:
            page_s, slot = str(key).split(":", 1)
            idx = int(page_s)
        except (ValueError, AttributeError):
            warnings.append(f"配图键格式不对：{key}（应为「页码:IMAGE_1」）")
            continue
        if not (1 <= idx <= len(pages)) or not str(prompt).strip():
            continue
        jobs.append((idx, slot.strip(), str(prompt).strip()))
    if not jobs:
        return template

    sem = asyncio.Semaphore(3)

    async def one(idx: int, slot: str, prompt: str) -> Tuple[int, str, Dict[str, Any]]:
        async with sem:
            stem = f"p{idx:02d}-{slot.lower().replace('_', '')}"
            res: Dict[str, Any]
            if use_mode == "generate":
                res = await asyncio.to_thread(
                    ppt_images.generate,
                    prompt=ppt_images.build_prompt(prompt),
                    out_dir=out_dir, filename=stem,
                    aspect_ratio="16:9", image_size="1K",
                    backend=use_backend, env=env,
                )
                if not res.get("ok") and cap["can_stock"]:
                    # 逐张降级：生图失败（额度/限流/被拒）不该让页面空着
                    fb = await asyncio.to_thread(
                        ppt_images.fetch_stock_tiered,
                        query=_stock_query(prompt), out_dir=out_dir,
                        filename=stem + "-stock", env=env,
                    )
                    if fb.get("ok"):
                        fb["degraded_from"] = res.get("error") or "生图失败"
                        return idx, slot, fb
                return idx, slot, res
            res = await asyncio.to_thread(
                ppt_images.fetch_stock_tiered,
                query=_stock_query(prompt), out_dir=out_dir,
                filename=stem, env=env,
            )
            return idx, slot, res

    results = await asyncio.gather(*[one(*j) for j in jobs], return_exceptions=True)
    new_pages: List[Dict[str, Any]] = [
        dict(p) if isinstance(p, dict) else {} for p in pages]
    credits: List[str] = []
    for item in results:
        if isinstance(item, BaseException):
            warnings.append(f"配图失败：{item}")
            continue
        idx, slot, res = item
        if not res.get("ok"):
            warnings.append(f"第 {idx} 页 {slot} 配图失败：{res.get('error')}")
            continue
        page = new_pages[idx - 1]
        values = dict(page.get("values") or {})
        values[slot] = str(res.get("path") or "")
        page["values"] = values
        if res.get("degraded_from"):
            warnings.append(
                f"第 {idx} 页 {slot} 生图失败（{res['degraded_from']}），已改用图库图片")
        # 需要署名的图（CC BY / CC BY-SA）：提示用户补出处，别默默用了侵权
        if res.get("attribution"):
            credits.append(f"第 {idx} 页 {slot}：{res['attribution']}")
    if credits:
        warnings.append("以下图片需要署名（请在图注/页脚补来源）：" + "；".join(credits))
    merged = dict(template)
    merged["pages"] = new_pages
    return merged


def ppt_plan_info(suite: str):
    """套件的可填充说明（独立函数便于测试替换）。

    技能缺失时抛 PPT_UNAVAILABLE，让上层返回 409（而不是 422「未知套件」）——
    用户看到的必须是「ppt-master 没装好」而不是「你选的套件不存在」。
    """
    from ...tools import ppt_templates

    avail = ppt_templates.availability()
    if not avail["ready"]:
        raise PptUnavailable(avail)
    return ppt_templates.describe_suite(suite)


async def _extract_slides(workspace: str, path: str):
    """读旧稿的逐页结构（复用 ppt_edit op=extract）。"""
    import json

    from ...tools.ppt_edit import PptEditTools

    tools = PptEditTools(workspace)
    raw = await tools.execute("ppt_edit", {"op": "extract", "path": path})
    if raw.startswith("[PPT Error]"):
        return None
    try:
        return json.loads(raw).get("slides") or None
    except json.JSONDecodeError:
        return None


async def _run_plan_llm(
    ctx: ServerContext, messages: list[dict[str, str]]
) -> tuple[str, str | None]:
    """单次规划调用，返回 (正文, 错误)。任何异常都转成可读错误，不冒泡。"""
    import asyncio
    import logging

    from ...core.types import Message

    logger = logging.getLogger("litework.server.ppt")
    try:
        adapter = ctx.app.adapter
        msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
        content, _, _ = await asyncio.wait_for(
            adapter.chat_stream(msgs, [], None), timeout=180)
        return (content or "").strip(), None
    except asyncio.TimeoutError:
        logger.warning("[ppt] 规划调用超时")
        return "", "规划超时（180s），可改用手动编排或重试"
    except Exception as exc:  # noqa: BLE001 - 失败要降级到兜底骨架，不能 500
        logger.warning("[ppt] 规划调用失败: %s", exc)
        return "", f"模型调用失败：{exc}"
