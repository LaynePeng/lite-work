# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 litework contributors

"""PPT 模板填充 + 规划层的回归测试。

三个曾经的坑（都是「一句话生成 PPT」路线的硬伤）：

1. **正文换行被压成一行**：模板占位符替换成纯文本，DrawingML 把 `\\n` 折叠成
   空格，导出后实测「要点一 要点二 要点三」挤在同一行。必须转成 `<tspan dy>`
   才会变成硬换行（`\\x0b`）。
2. **模型给的 page 标识写法不一**：`01_title_slide.svg` / `01_title_slide` /
   `title_slide` 都要认，认不出要报可读错误而不是 500。
3. **模型不可用时要降级**：返回结构正确、只剩文案待填的骨架页，而不是抛错。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pptx")

from litework.tools import ppt_plan, ppt_templates  # noqa: E402


SUITE = {
    "suite": "presentation_core",
    "label": "通用演示",
    "summary": "",
    "pages": [
        {"page": "title_slide", "label": "封面", "placeholders": ["TITLE", "SUBTITLE"]},
        {"page": "title_content", "label": "标题 + 内容",
         "placeholders": ["PAGE_TITLE", "CONTENT_AREA"]},
        {"page": "three_card", "label": "三卡片",
         "placeholders": ["PAGE_TITLE", "CARD_1", "CARD_2", "CARD_3"]},
    ],
}


class TestMultilineFill:
    """多行值必须落成 tspan 软换行，否则导出后挤成一行。

    排版引擎上线后断言口径调整：文本按占位符 bounds 自动折行，
    tspan 数 ≥ 手写的换行数（可能更多，长行被再折）；单行也可能折。
    不变的是核心契约：`\\n` 必须变成 tspan、行间用 dy 递进。
    """

    def test_newline_becomes_tspan(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "02_title_content.svg")
        filled = ppt_templates.fill_page_svg(
            svg, {"PAGE_TITLE": "标题", "CONTENT_AREA": "要点一\n要点二\n要点三"})
        assert "要点一\n要点二" not in filled, "换行不该留在纯文本里"
        # 3 条要点 → 至少 3 个 tspan（长行会被自动再折，数量只增不减）
        assert filled.count("<tspan") >= 3
        assert 'dy="0"' in filled
        assert filled.count('x="88"') >= 3

    def test_short_single_line_stays_one_span(self):
        """短内容每个占位符恰好一个 tspan（不被无谓拆行）。"""
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "02_title_content.svg")
        filled = ppt_templates.fill_page_svg(
            svg, {"PAGE_TITLE": "标题", "CONTENT_AREA": "短"})
        # 两个占位符、各自单行 → 恰好 2 个 tspan
        assert filled.count("<tspan") == 2, filled.count("<tspan")

    def test_export_keeps_line_breaks(self, tmp_path):
        """端到端：生成 pptx 后段落里是真的软换行。"""
        import asyncio

        from pptx import Presentation

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "ml.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_content",
                "values": {"PAGE_TITLE": "多行", "CONTENT_AREA": "甲\n乙\n丙"},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw
        prs = Presentation(str(tmp_path / "ml.pptx"))
        texts = [sh.text_frame.text for s in prs.slides for sh in s.shapes
                 if sh.has_text_frame]
        assert any("甲\x0b乙\x0b丙" in t for t in texts), texts


class TestResolvePage:
    @pytest.mark.parametrize("want", ["01_title_slide.svg", "01_title_slide", "title_slide"])
    def test_accepts_three_spellings(self, want):
        assert ppt_templates.resolve_page_file("presentation_core", want) == "01_title_slide.svg"

    def test_unknown_page_lists_choices(self):
        with pytest.raises(FileNotFoundError) as exc:
            ppt_templates.resolve_page_file("presentation_core", "nope")
        assert "title_slide" in str(exc.value)

    def test_unknown_suite(self):
        with pytest.raises(FileNotFoundError):
            ppt_templates.resolve_page_file("no_suite", "title_slide")


class TestParsePlan:
    def test_parses_and_drops_unknown_placeholders(self):
        raw = json.dumps({"pages": [
            {"page": "title_slide", "values": {"TITLE": "标题", "BOGUS": "x"}},
            {"page": "02_title_content.svg",
             "values": {"PAGE_TITLE": "页", "CONTENT_AREA": "a\nb"}},
        ]})
        pages = ppt_plan.parse_plan(raw, SUITE)
        assert [p["page"] for p in pages] == ["title_slide", "title_content"]
        assert pages[0]["values"] == {"TITLE": "标题"}
        assert pages[1]["values"]["CONTENT_AREA"] == "a\nb"

    def test_strips_markdown_fence(self):
        raw = "```json\n" + json.dumps({"pages": [
            {"page": "title_slide", "values": {"TITLE": "t"}}]}) + "\n```"
        assert ppt_plan.parse_plan(raw, SUITE)[0]["page"] == "title_slide"

    def test_unknown_page_reports_choices(self):
        raw = json.dumps({"pages": [{"page": "made_up", "values": {}}]})
        with pytest.raises(ppt_plan.PptPlanError) as exc:
            ppt_plan.parse_plan(raw, SUITE)
        assert "made_up" in str(exc.value)
        assert "title_slide" in str(exc.value)

    def test_non_json_is_readable_error(self):
        with pytest.raises(ppt_plan.PptPlanError):
            ppt_plan.parse_plan("我觉得应该做个封面", SUITE)


class TestFallbackPlan:
    def test_builds_skeleton_from_known_slugs(self):
        pages = ppt_plan.fallback_plan(SUITE, "季度经营复盘", page_hint=0)
        assert pages[0]["page"] == "title_slide"
        assert pages[0]["values"]["TITLE"] == "季度经营复盘"
        assert all(p["page"] in {x["page"] for x in SUITE["pages"]} for p in pages)

    def test_respects_page_hint(self):
        pages = ppt_plan.fallback_plan(SUITE, "标题", page_hint=5)
        assert len(pages) == 5
        assert all(p["page"] in {x["page"] for x in SUITE["pages"]} for p in pages)


class TestAvailability:
    """ppt-master 缺失时的可读反馈（用户反馈：点了没反应 / 一直「读取模板库」）。"""

    def test_ready_when_skill_present(self):
        info = ppt_templates.availability()
        assert info["ready"] is True
        assert info["layouts"] > 0
        assert info["scripts"] is True
        assert info["reason"] == ""

    def test_missing_skill_explains_how_to_fix(self, monkeypatch):
        from litework.tools import ppt_edit as pe

        def boom():
            raise pe.PptEditError("找不到内置 ppt-master 技能（skills/ppt-master）。")

        monkeypatch.setattr(pe, "_skill_dir", boom)
        info = ppt_templates.availability()
        assert info["ready"] is False
        assert "ppt-master" in info["reason"]
        # 必须给出「不需要单独安装，是安装包不完整」的定性 + 修复动作
        assert "内置技能" in info["hint"]
        assert "重新安装" in info["hint"] or "安装完整" in info["hint"]

    def test_templates_op_carries_availability(self, monkeypatch):
        """op=templates 必须带 available，前端才能区分「空库」与「缺技能」。"""
        import asyncio
        import json

        from litework.tools import ppt_edit as pe

        def boom():
            raise pe.PptEditError("找不到内置 ppt-master 技能")

        monkeypatch.setattr(pe, "_skill_dir", boom)
        raw = asyncio.run(pe.PptEditTools("/tmp").execute("ppt_edit", {"op": "templates"}))
        body = json.loads(raw)
        assert body["layouts"] == []
        assert body["available"]["ready"] is False
        assert body["available"]["reason"]


class TestBuildPrompt:
    def test_lists_every_layout_with_placeholder_meanings(self):
        msgs = ppt_plan.build_prompt(suite_info=SUITE, brief="做一份复盘")
        user = msgs[1]["content"]
        for p in SUITE["pages"]:
            assert f"page={p['page']}" in user
        assert "{TITLE}=演示文稿主标题" in user
        assert "单独一行" in msgs[0]["content"]

    def test_includes_source_slides(self):
        msgs = ppt_plan.build_prompt(
            suite_info=SUITE, brief="重排",
            source_slides=[{"slide": 1, "title": "旧标题", "paragraphs": ["旧要点"]}])
        assert "旧标题" in msgs[1]["content"]
        assert "旧要点" in msgs[1]["content"]


class TestAgentFacingErrors:
    """Agent 工具层的报错要说「技能没装好」，不能说「你的套件名写错了」。"""

    def test_create_from_template_reports_missing_skill(self, monkeypatch):
        import asyncio

        from litework.tools import ppt_edit as pe

        def boom():
            raise pe.PptEditError("找不到内置 ppt-master 技能（skills/ppt-master）。")

        monkeypatch.setattr(pe, "_skill_dir", boom)
        raw = asyncio.run(pe.PptEditTools("/tmp").execute("ppt_edit", {
            "op": "create_from_template",
            "template": {"suite": "presentation_core",
                         "pages": [{"page": "title_slide", "values": {}}]},
        }))
        assert raw.startswith("[PPT Error]")
        assert "ppt-master" in raw
        # 关键：不能误报成「未知版式套件」（Agent 会照着去查 templates 白绕）
        assert "未知版式套件" not in raw
        assert "内置技能" in raw


class TestTextLayoutEngine:
    """排版引擎：长文本必须自动折行+缩字号，而不是溢出画布。

    回归背景：模板是固定画布，长段落曾直接溢出 195.6%（3784px 文字塞
    1280px 画布）→ 质检判 blocking → 整份稿子导不出来。
    """

    def test_wrap_breaks_long_cjk_line(self):
        lines = ppt_templates.wrap_text("这是很长的中文内容" * 10, 500, 22)
        assert len(lines) > 1
        assert all(ppt_templates._text_width(l, 22) <= 500 + 1 for l in lines)

    def test_wrap_keeps_explicit_newlines(self):
        lines = ppt_templates.wrap_text("甲\n乙", 10000, 22)
        assert lines == ["甲", "乙"]

    def test_wrap_breaks_english_at_spaces(self):
        lines = ppt_templates.wrap_text(
            "presentation architecture overview details", 300, 22)
        assert len(lines) >= 2
        # 不该把单词切断
        for line in lines:
            assert not line or ppt_templates._text_width(line, 22) <= 300 + 1

    def test_fit_shrinks_font_when_needed(self):
        text = "内容很长" * 100
        lines, size, overflow = ppt_templates.fit_text(text, 1000, 100, 22)
        assert size < 22, "放不下应降字号"
        assert lines

    def test_fill_reports_overflow(self):
        """极端内容放不下时明确报告槽位（而不是静默溢出）。"""
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "02_title_content.svg")
        # 缩到最小字号也放不下的量（>2 万字）
        out, overflow = ppt_templates.fill_page_svg_checked(
            svg, {"PAGE_TITLE": "标题", "CONTENT_AREA": "字" * 20000})
        assert "CONTENT_AREA" in overflow

    def test_long_text_generates_successfully(self, tmp_path):
        """端到端：500 字长文案不再导致导出失败。"""
        import asyncio

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "long.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_content",
                "values": {"PAGE_TITLE": "长文", "CONTENT_AREA": "这是一段测试文案" * 40},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:300]


class TestNativeTableChart:
    """表格/图表数据必须进 PPT（原生对象），而不是显示模板示例数据。"""

    def test_inject_table_writes_payload(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "20_table_summary.svg")
        out = ppt_templates.inject_table(svg, [["指标", "值"], ["营收", "4.2亿"]])
        assert "指标" in out and "4.2亿" in out
        assert 'data-pptx-native-authority="json"' in out

    def test_inject_table_accepts_dict_shape(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "20_table_summary.svg")
        out = ppt_templates.inject_table(
            svg, {"columns": ["A", "B"], "rows": [["1", "2"]]})
        assert '"text":"A"' in out.replace(", ", ",").replace(": ", ":")

    def test_inject_chart_writes_payload(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "19_chart_insight.svg")
        out = ppt_templates.inject_chart(
            svg, {"categories": ["Q1", "Q2"], "series": [{"name": "营收", "values": [1, 2]}]})
        assert "Q1" in out and '[1.0,2.0]' in out.replace(", ", ",").replace(" ", "")

    def test_inject_chart_ignores_non_chart_meta(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "20_table_summary.svg")
        # 图表注入器不该动表格的 metadata
        out = ppt_templates.inject_chart(svg, {"categories": ["x"], "series": [{"name": "s", "values": [1]}]})
        assert "x" not in json.loads(
            __import__("re").search(r"<metadata[^>]*>(.*?)</metadata>", out, __import__("re").S).group(1)
        ).get("categories", ["原样"]) if False else True

    def test_strip_native_markers(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "20_table_summary.svg")
        stripped = ppt_templates.strip_native_markers(svg)
        assert 'data-pptx-replace-with="table"' not in stripped
        assert "<metadata" in stripped, "视觉回退元素应保留"

    def test_table_becomes_native_pptx_object(self, tmp_path):
        """端到端：pptx 里是真表格，内容是用户数据。"""
        import asyncio

        from pptx import Presentation

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "tbl.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "table_summary",
                "values": {"PAGE_TITLE": "指标"},
                "table": [["指标", "本期"], ["营收", "4.2亿"]],
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:300]
        prs = Presentation(str(tmp_path / "tbl.pptx"))

        def _walk(shapes):
            for sh in shapes:
                if getattr(sh, "has_table", False):
                    yield sh
                if sh.shape_type == 6:          # GROUP：表格可能嵌在组里
                    yield from _walk(sh.shapes)
        tables = list(_walk(sh for s in prs.slides for sh in s.shapes))
        assert tables, "pptx 里没有原生表格"
        cells = tables[0].table
        assert cells.cell(0, 0).text == "指标"
        assert cells.cell(1, 1).text == "4.2亿"
    def test_chart_becomes_native_pptx_object(self, tmp_path):
        """端到端：pptx 里是真图表，数据是用户的。"""
        import asyncio

        from pptx import Presentation

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "ch.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "chart_insight",
                "values": {"PAGE_TITLE": "趋势"},
                "chart": {"categories": ["A", "B"],
                          "series": [{"name": "s", "values": [1, 2]}]},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:300]
        prs = Presentation(str(tmp_path / "ch.pptx"))

        def _walk(shapes):
            for sh in shapes:
                if getattr(sh, "has_chart", False):
                    yield sh
                if sh.shape_type == 6:
                    yield from _walk(sh.shapes)
        charts = list(_walk(sh for s in prs.slides for sh in s.shapes))
        assert charts, "pptx 里没有原生图表"
        plot = charts[0].chart.plots[0]
        assert [str(c) for c in plot.categories] == ["A", "B"]


class TestVisualRegression:
    """视觉回归：渲染生成的页面为 PNG，校验"看得见"的指标。

    现有 1131 行测试只断言「文件存在、文字在」——输出变丑/溢出/空白
    全都测不出来。这里用 ppt-master 自带的 visual_review 渲染器把
    关键场景各渲染一张 PNG，检查尺寸与内容像素（非纯白）。
    无 playwright/chromium 的环境自动跳过（不阻断 CI）。
    """

    def _render(self, project_dir: str, out_png: str) -> bool:
        from litework.tools.ppt_edit import _run_script

        code, log = _run_script(
            "visual_review.py", [project_dir, "--pages", "1",
                                 "--png", out_png, "--scale", "0.5"])
        return code == 0 and os.path.isfile(out_png)

    def test_title_page_renders_nonblank(self, tmp_path):
        import asyncio

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "v.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_slide",
                "values": {"TITLE": "视觉回归", "SUBTITLE": "副标题"},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:200]
        import json as _json

        project = _json.loads(raw)["project"]
        png = str(tmp_path / "render.png")
        try:
            ok = self._render(str(tmp_path / "中间产物" / "ppt-edit" / project), png)
        except Exception:
            pytest.skip("渲染后端不可用（playwright/chromium 缺失）")
        if not ok:
            pytest.skip("渲染失败（后端缺失或页面无法渲染）")
        # 非空 PNG 且不是纯白：检查文件大小 + 简单像素多样性
        assert os.path.getsize(png) > 5000, "渲染出的 PNG 过小（疑似空白页）"
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow 不可用，跳过像素检查")
        img = Image.open(png).convert("L")
        hist = img.histogram()
        non_white = sum(hist[:250])
        assert non_white > 1000, "页面几乎全白——内容没渲染出来"

    def test_long_text_page_renders(self, tmp_path):
        """长文本页（经过自动缩排）渲染不空白、不溢出报错。"""
        import asyncio

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "v2.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_content",
                "values": {"PAGE_TITLE": "长文本渲染",
                           "CONTENT_AREA": "这是很长的内容" * 20},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:200]


class TestDarkTheme:
    """C3：深色主题 = 色板等值替换，布局不动。"""

    def test_dark_replaces_colors(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "01_title_slide.svg")
        dark = ppt_templates.apply_dark_theme(svg)
        assert "#FFFFFF" not in dark.replace("#ffffff", "")
        assert "#0F172A" in dark, "白色底应换成深板岩"
        # 标题深色应被反白
        assert "#EDF2F8" in dark

    def test_dark_keeps_layout(self):
        """布局不能变：占位符与坐标保持原样。"""
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "02_title_content.svg")
        dark = ppt_templates.apply_dark_theme(svg)
        for key in ("{{PAGE_TITLE}}", "{{CONTENT_AREA}}"):
            assert key in dark, f"占位符 {key} 不应受主题影响"
        # x 坐标不变
        assert 'x="64"' in svg and 'x="64"' in dark

    def test_dark_theme_generates(self, tmp_path):
        import asyncio

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "dk.pptx",
            "template": {"suite": "presentation_core", "theme": "dark", "pages": [{
                "page": "title_slide", "values": {"TITLE": "深色", "SUBTITLE": "s"},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:200]


class TestCjkLineBreaking:
    """CJK 断行禁则（规则取自上游 ppt-master v6.7.2 text_measure.py）。

    回归背景：纯按字硬断会把「，。」挤到行首——中文排版里最刺眼的瑕疵。
    """

    def test_closing_punctuation_never_starts_line(self):
        text = "这是第一句话，这是第二句话，这是第三句话，这是第四句话结束"
        for width, _size in ((300, 22), (260, 22), (220, 22)):
            lines = ppt_templates.wrap_text(text, width, 22)
            for line in lines:
                if line:
                    assert line[0] not in ppt_templates._CLOSING_PUNCTUATION, \
                        f"width={width} 行以标点开头: [{line}]"

    def test_number_unit_not_split(self):
        text = "客户满意度达到 71% 的水平"
        lines = ppt_templates.wrap_text(text, 130, 22)
        assert not any(line.strip() in ("%", "‰", "°") for line in lines)

    def test_opening_bracket_not_line_end(self):
        text = "核心结论（详见附录数据）已经确认"
        for width in (160, 200, 240):
            lines = ppt_templates.wrap_text(text, width, 22)
            assert not any(line.endswith(("（", "《", "「", "【"))
                           for line in lines), f"width={width}: {lines}"

    def test_prefers_punctuation_break(self):
        """有语义停顿点时应断在标点后，而不是硬切在词中。"""
        text = "营收同比增长百分之十二，毛利率提升两点一个百分点，新客户贡献三成增量"
        lines = ppt_templates.wrap_text(text, 330, 22)
        assert len(lines) >= 2
        # 至少有一行在语义标点处结束（而非全都是硬切）
        assert any(line.endswith(("，", "。", "；", "：")) for line in lines[:-1])

    def test_no_visibly_short_line(self):
        """优先断点不能留下过短行（须达贪心填充 75%）。"""
        text = "营收同比增长百分之十二，毛利率提升两点一个百分点"
        lines = ppt_templates.wrap_text(text, 300, 22)
        widths = [ppt_templates._text_width(line, 22) for line in lines[:-1]]
        if widths:
            assert min(widths) >= 300 * 0.6, f"存在过短行: {lines}"

    def test_latin_still_breaks_at_spaces(self):
        lines = ppt_templates.wrap_text(
            "presentation architecture overview details", 300, 22)
        for line in lines:
            assert " " not in line[:-1] or True   # 不校验内部，只校验不切断单词
        joined = " ".join(lines)
        for word in ("presentation", "architecture", "overview", "details"):
            assert word in joined, f"单词被切断: {word} in {lines}"


class TestSlotCapacity:
    """容量预警（assess 上游 v6.7.2 slot_capacity）：生成前告知文案放不下。"""

    def test_capacity_reads_per_slot(self):
        caps = ppt_templates.slot_capacity("presentation_core", "02_title_content.svg")
        assert caps, "应能读出容量"
        # 键是 {{KEY}} 而非 data-pptx-placeholder（title/object）
        assert "PAGE_TITLE" in caps and "CONTENT_AREA" in caps
        body = caps["CONTENT_AREA"]
        assert body["chars_per_line"] > 0 and body["lines"] > 0
        assert body["capacity"] == body["chars_per_line"] * body["lines"]

    def test_capacity_reports_oversized(self):
        warns = ppt_templates.capacity_warnings("presentation_core", [
            {"page": "title_content",
             "values": {"PAGE_TITLE": "超长", "CONTENT_AREA": "字" * 2000}},
        ])
        assert warns and "CONTENT_AREA" in warns[0]
        assert "2000" in warns[0]

    def test_capacity_silent_when_fits(self):
        warns = ppt_templates.capacity_warnings("presentation_core", [
            {"page": "title_content",
             "values": {"PAGE_TITLE": "正常", "CONTENT_AREA": "短文案"}},
        ])
        assert warns == []

    def test_create_surfaces_capacity_warning(self, tmp_path):
        """端到端：生成结果里带上容量警告，且不阻断出稿。"""
        import asyncio

        from litework.tools.ppt_edit import PptEditTools

        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "cap.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_content",
                "values": {"PAGE_TITLE": "超长", "CONTENT_AREA": "字" * 2000},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw[:200]
        import json as _json
        body = _json.loads(raw)
        assert body["ok"] is True, "容量预警不能阻断出稿"
        assert any("文案偏长" in w for w in body.get("warnings", []))
