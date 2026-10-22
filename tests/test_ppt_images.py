# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 litework contributors

"""PPT 配图：模板图片位填充 + 生图后端凭据映射。

两个曾经的坑：

1. **模板图片位永远出不了图**：`fill_page_svg` 只替换 `{{TEXT}}`，而图片位是
   `<image>`（带 1×1 透明占位图）——21 个带图版式此前填了也白填。
2. **配置重复**：生图后端读环境变量，而用户密钥在 config.json；不映射就得
   让用户为配图再填一遍 key。
"""
from __future__ import annotations

import base64
import os
import zlib
import struct

import pytest

from litework.tools import ppt_images, ppt_templates


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class TestImageSlots:
    def test_lists_slots_per_page(self):
        for lay in ppt_templates.list_layouts():
            for p in lay["pages"]:
                slots = p.get("image_slots") or []
                if slots:
                    assert slots[0]["key"] == "IMAGE_1"
                    assert slots[0]["element_id"]

    def test_two_picture_page_has_two_slots(self):
        lay = next(x for x in ppt_templates.list_layouts() if x["suite"] == "presentation_core")
        page = next(p for p in lay["pages"] if p["slug"] == "two_picture_caption")
        assert [s["key"] for s in page["image_slots"]] == ["IMAGE_1", "IMAGE_2"]

    def test_text_only_page_has_no_slots(self):
        lay = next(x for x in ppt_templates.list_layouts() if x["suite"] == "presentation_core")
        page = next(p for p in lay["pages"] if p["slug"] == "title_content")
        assert page["image_slots"] == []


class TestFillImages:
    """图片位必须真的被替换掉 1×1 占位图。"""

    def test_path_becomes_inlined_data_uri(self, tmp_path):
        img = tmp_path / "hero.png"
        img.write_bytes(_png(64, 36, (200, 60, 40)))

        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "16_title_picture.svg")
        out = ppt_templates.fill_page_svg(
            svg, {"PAGE_TITLE": "标题", "IMAGE_1": "hero.png"}, workspace=str(tmp_path))

        # 占位图（1×1）被换成真图的 data URI
        assert "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC" not in out
        b64 = base64.b64encode(img.read_bytes()).decode()
        assert b64[:40] in out
        assert 'preserveAspectRatio="none"' in out, "原有定位属性不能被破坏"

    def test_relative_path_without_workspace_raises(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "16_title_picture.svg")
        with pytest.raises(FileNotFoundError):
            ppt_templates.fill_page_svg(svg, {"IMAGE_1": "hero.png"})

    def test_missing_file_raises_readable(self, tmp_path):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "16_title_picture.svg")
        with pytest.raises(FileNotFoundError) as exc:
            ppt_templates.fill_page_svg(svg, {"IMAGE_1": "nope.png"}, workspace=str(tmp_path))
        assert "nope.png" in str(exc.value)

    def test_two_slots_fill_independently(self, tmp_path):
        # 两张图用不同尺寸，保证 base64 前缀可区分（同尺寸纯色图压缩后极像）
        (tmp_path / "a.png").write_bytes(_png(40, 22, (10, 200, 10)))
        (tmp_path / "b.png").write_bytes(_png(56, 31, (10, 10, 200)))
        svg, _ = ppt_templates.prepare_page_svg(
            "presentation_core", "17_two_picture_caption.svg")
        out = ppt_templates.fill_page_svg(
            svg, {"PAGE_TITLE": "双图", "IMAGE_1": "a.png", "IMAGE_2": "b.png"},
            workspace=str(tmp_path))
        a = base64.b64encode((tmp_path / "a.png").read_bytes()).decode()
        b = base64.b64encode((tmp_path / "b.png").read_bytes()).decode()
        assert a[:80] in out and b[:80] in out
        # 两张都已替换：占位图不再出现
        assert "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC" not in out

    def test_only_first_slot_filled(self, tmp_path):
        (tmp_path / "a.png").write_bytes(_png(32, 18, (10, 200, 10)))
        svg, _ = ppt_templates.prepare_page_svg(
            "presentation_core", "17_two_picture_caption.svg")
        out = ppt_templates.fill_page_svg(
            svg, {"PAGE_TITLE": "双图", "IMAGE_1": "a.png"}, workspace=str(tmp_path))
        # 第二个位置仍留着占位图，不会整页失败
        assert "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC" in out

    def test_data_uri_passes_through(self):
        svg, _ = ppt_templates.prepare_page_svg("presentation_core", "16_title_picture.svg")
        uri = "data:image/png;base64,AAAA"
        out = ppt_templates.fill_page_svg(svg, {"IMAGE_1": uri})
        assert uri in out

    def test_export_puts_real_image_into_pptx(self, tmp_path):
        """端到端：填入的图在 pptx 里是原生图片对象，且尺寸正确。"""
        import asyncio

        from pptx import Presentation

        from litework.tools.ppt_edit import PptEditTools

        (tmp_path / "hero.png").write_bytes(_png(1024, 576, (180, 40, 90)))
        tools = PptEditTools(str(tmp_path))
        raw = asyncio.run(tools.execute("ppt_edit", {
            "op": "create_from_template", "filename": "deck.pptx",
            "template": {"suite": "presentation_core", "pages": [{
                "page": "title_picture",
                "values": {"PAGE_TITLE": "封面", "IMAGE_1": "hero.png"},
            }]},
        }))
        assert not raw.startswith("[PPT Error]"), raw

        prs = Presentation(str(tmp_path / "deck.pptx"))
        pics = [sh for s in prs.slides for sh in s.shapes
                if sh.shape_type == 13]
        nested = [sh for s in prs.slides for sh in s.shapes if sh.shape_type == 6
                  for sh in sh.shapes if sh.shape_type == 13]
        found = pics + nested
        assert found, "pptx 里没有图片对象"
        assert any(sh.image.size == (1024, 576) for sh in found)


class TestImageBackendMapping:
    def test_maps_chat_providers_to_backends(self):
        env = ppt_images._skill_env({
            "openai": {"api_key": "sk-o"},
            "qwen": {"api_key": "sk-q"},
            "glm": {"api_key": "sk-g"},
        })
        assert env["OPENAI_API_KEY"] == "sk-o"
        assert env["QWEN_API_KEY"] == "sk-q"
        assert env["ZHIPU_API_KEY"] == "sk-g"

    def test_masked_key_is_not_injected(self):
        env = ppt_images._skill_env({"openai": {"api_key": "sk-…abcd"}})
        assert not env.get("OPENAI_API_KEY", "").startswith("sk-…")

    def test_chat_model_name_does_not_become_image_model(self):
        """gpt-5.6 不是生图模型名，不能被当作 OPENAI_MODEL 传下去。"""
        env = ppt_images._skill_env({
            "openai": {"api_key": "sk-o", "model": "gpt-5.6"}})
        assert env.get("OPENAI_MODEL") is None

    def test_available_backends_marks_provider_source(self):
        out = ppt_images.available_backends({"openai": {"api_key": "sk-o"}})
        assert out
        hit = next(b for b in out if b["backend"] == "openai")
        assert hit["from_llm_provider"] is True
        assert hit["label"] == "OpenAI"

    def test_no_credentials_means_no_backends(self, monkeypatch):
        for key in ("OPENAI_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
                    "ZHIPU_API_KEY", "BIGMODEL_API_KEY", "GEMINI_API_KEY",
                    "GOOGLE_API_KEY", "BFL_API_KEY", "IDEOGRAM_API_KEY",
                    "STABILITY_API_KEY", "TENCENT_API_KEY", "TOKENHUB_API_KEY",
                    "FAL_KEY", "FAL_API_KEY", "MINIMAX_API_KEY",
                    "MODELSCOPE_API_KEY", "OPENROUTER_API_KEY",
                    "REPLICATE_API_TOKEN", "REPLICATE_API_KEY",
                    "SILICONFLOW_API_KEY", "LAS_API_KEY", "VOLCENGINE_API_KEY",
                    "ARK_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        assert ppt_images.available_backends({}) == []


class TestPrompt:
    def test_forbids_text_in_image(self):
        """生图模型渲染文字必错，提示词必须显式禁止。"""
        p = ppt_images.build_prompt("modern office meeting")
        assert "modern office meeting" in p
        assert "no text" in p.lower()

    def test_keeps_style_and_mood(self):
        p = ppt_images.build_prompt("a factory", "cinematic", "warm tones")
        assert "a factory" in p and "cinematic" in p and "warm tones" in p


class TestStockFallback:
    """没有生图模型时要能降级到开放图库，而不是「功能不可用」。"""

    def test_zero_config_providers_always_available(self):
        out = ppt_images.available_stock_providers()
        names = {p["provider"] for p in out}
        assert "openverse" in names and "wikimedia" in names
        ov = next(p for p in out if p["provider"] == "openverse")
        assert ov["zero_config"] is True
        assert ov["no_attribution"] is True, "Openverse 可筛 CC0，不用在页面署名"

    def test_keyed_providers_hidden_without_key(self, monkeypatch):
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
        names = {p["provider"] for p in ppt_images.available_stock_providers()}
        assert "pexels" not in names

    def test_capability_is_stock_without_image_model(self):
        """核心：没有生图凭据时 mode=stock，而不是 none。"""
        cap = ppt_images.capability({})
        assert cap["can_generate"] is False
        assert cap["can_stock"] is True
        assert cap["mode"] == "stock"

    def test_capability_prefers_generate_with_credentials(self):
        cap = ppt_images.capability({"openai": {"api_key": "sk-o"}})
        assert cap["can_generate"] is True
        assert cap["mode"] == "generate", "配了生图就该优先生图（更贴合内容）"

    def test_stock_query_strips_generation_constraints(self):
        from litework.server.routers.ppt import _stock_query

        q = _stock_query("aerial view of logistics warehouse, no text, no watermark, "
                         "high quality")
        assert "no text" not in q and "watermark" not in q
        assert "aerial" in q and "logistics" in q
        assert len(q.split()) <= 4, "图库是全文检索，长句命中率低"

    def test_explain_stock_is_actionable(self):
        msg = ppt_images._explain_stock(
            "no acceptable candidate across all providers/stages (12 candidate(s) failed)")
        assert "换" in msg or "放宽" in msg

    def test_attribution_detected(self):
        log = "  picked: 'Wind Turbine.jpg' from wikimedia (CC BY-SA 2.0, attribution-required)"
        assert "CC BY-SA" in ppt_images._attribution_of(log)
        # CC0 不需要署名
        log2 = "  picked: 'Business Office' from openverse (CC0, no-attribution)"
        assert ppt_images._attribution_of(log2) == ""


@pytest.mark.skipif(
    os.environ.get("LITEWORK_SKIP_NETWORK_TESTS") == "1",
    reason="显式跳过网络用例",
)
class TestStockLive:
    """真实检索（openverse 零密钥）——确认兜底路径真的能拿到图。"""

    def test_openverse_returns_downloadable_image(self, tmp_path):
        res = ppt_images.search_stock(
            query="business meeting office", out_dir=str(tmp_path),
            filename="probe", provider="openverse",
            min_width=800, min_height=450, timeout=90)
        if not res.get("ok"):
            pytest.skip(f"网络不可用或无结果：{res.get('error')}")
        assert os.path.isfile(res["path"])
        assert os.path.getsize(res["path"]) > 1000
        assert res["provider"] == "openverse"


class TestProxyInjection:
    """配图子进程必须继承用户配置的代理（否则需要代理的网络上直接失败）。

    生图/图库脚本用 `requests`，它只认 HTTP_PROXY/HTTPS_PROXY 环境变量——
    httpx 能靠参数传代理，requests 不能。漏了注入就会绕过代理直连。
    """

    _VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
             "NO_PROXY", "no_proxy")

    def _clean(self, monkeypatch):
        for k in self._VARS:
            monkeypatch.delenv(k, raising=False)

    def test_injects_proxy_when_media_scope_enabled(self, monkeypatch):
        self._clean(monkeypatch)
        cfg = {"proxy": {"enabled": True, "url": "http://127.0.0.1:7890",
                         "scope": ["media"], "bypass": ["internal.corp"]}}
        env = ppt_images._skill_env(None, cfg)
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
        assert env["http_proxy"] == "http://127.0.0.1:7890", "小写变体也要有（requests 认它）"
        assert env["NO_PROXY"] == "internal.corp"

    def test_no_injection_when_scope_excluded(self, monkeypatch):
        self._clean(monkeypatch)
        cfg = {"proxy": {"enabled": True, "url": "http://127.0.0.1:7890",
                         "scope": ["llm", "webfetch"]}}
        env = ppt_images._skill_env(None, cfg)
        assert not env.get("HTTPS_PROXY")

    def test_clears_stale_env_when_proxy_disabled(self, monkeypatch):
        """应用里关了代理，就不能因为 shell 有 HTTPS_PROXY 而偷偷走代理。"""
        self._clean(monkeypatch)
        monkeypatch.setenv("HTTPS_PROXY", "http://stale:3128")
        monkeypatch.setenv("https_proxy", "http://stale:3128")
        cfg = {"proxy": {"enabled": False, "url": "http://127.0.0.1:7890",
                         "scope": ["media"]}}
        env = ppt_images._skill_env(None, cfg)
        assert not env.get("HTTPS_PROXY")
        assert not env.get("https_proxy")

    def test_direct_call_also_gets_proxy(self, monkeypatch):
        """直接调 search_stock/generate（不传 env）也必须带代理。"""
        self._clean(monkeypatch)
        cfg = {"proxy": {"enabled": True, "url": "http://127.0.0.1:7890",
                         "scope": ["media"]}}
        env = ppt_images._spawn_env(None, cfg)
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"

    def test_explicit_env_wins(self, monkeypatch):
        """路由层已构造好的 env 原样使用（不重复注入）。"""
        env_in = {"HTTPS_PROXY": "http://already:8080"}
        assert ppt_images._spawn_env(env_in, None)["HTTPS_PROXY"] == "http://already:8080"

    def test_media_scope_registered(self):
        from litework.core.proxy import PROXY_SCOPES, SCOPE_LABELS
        assert "media" in PROXY_SCOPES
        assert "media" in SCOPE_LABELS
