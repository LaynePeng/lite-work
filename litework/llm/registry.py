# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""LLM 供应商注册表：管理多供应商配置、构建适配器、测试连接。"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .anthropic import AnthropicAdapter
from .base import BaseLLMAdapter
from .openai_compat import OpenAICompatAdapter

logger = logging.getLogger("litework.llm")

# 预置供应商元数据
PROVIDER_META: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek",
        "kind": "openai",
        "default_base_url": "https://api.deepseek.com",
        # 官方现行名（2026-09 定价页 MODEL 行只有这两个）：旧名 deepseek-v4-flash /
        # vision-exp 已被官方下线，请求由 V4.1-Flash 承接并按 Flash 计费，故不再
        # 出现在可选列表（旧配置仍能解析窗口与价格，见 context_windows /
        # 定价插件 pricing-plugin 的别名表）。分时计费（高峰/空闲半价）见该插件。
        "default_model": "deepseek-flash",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "env_key": "DEEPSEEK_API_KEY",
        "context_window": 1_000_000,
        "context_windows": {
            "deepseek-flash": 1_000_000,
            "deepseek-v4-pro": 1_000_000,
            # 历史别名：旧配置仍用旧名时也能解析出窗口（不再出现在模型列表里）
            "deepseek-v4-flash": 1_000_000,
            "deepseek-v4-flash-vision-exp": 1_000_000,
        },
        # V4.1-Flash / V4-Pro 默认即思考模式（可在设置里收紧 effort）
        "reasoning_models": ["deepseek-flash", "deepseek-v4-pro", "deepseek-reasoner"],
    },
    "openai": {
        "name": "OpenAI",
        "kind": "openai",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.6",
        "models": ["gpt-5.6", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini", "o4-mini"],
        "env_key": "OPENAI_API_KEY",
        "context_window": 128_000,
        "context_windows": {
            "gpt-5.6": 1_050_000,
            "gpt-5.6-luna": 1_050_000,
            "gpt-5.5": 1_050_000,
            "gpt-5.4-mini": 400_000,
            "o4-mini": 200_000,
            # 历史别名（老配置仍可解析窗口与价格）
            "gpt-4o": 128_000,
            "gpt-4o-mini": 128_000,
            "gpt-4.1": 1_047_576,
            "gpt-4.1-mini": 1_047_576,
            "o3-mini": 200_000,
        },
        # 支持 reasoning_effort 的模型（o 系列 / gpt-5 系列推理模型）
        "reasoning_models": ["gpt-5.6", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini",
                             "gpt-5", "gpt-5-mini", "o3", "o3-mini", "o4-mini",
                             "o1", "o1-mini", "o1-preview"],
    },
    "kimi": {
        "name": "Kimi (Moonshot)",
        "kind": "openai",
        # 国内站端点（老用户配置不动）；计费单价取**国际站官方定价**
        # （platform.kimi.ai，USD，见定价插件 pricing-plugin），与国内站
        # CNY 价有约 7% 偏差，可在设置里用 pricing_overrides 覆盖。
        "default_base_url": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k3",
        "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
        "env_key": "MOONSHOT_API_KEY",
        "context_window": 262_144,
        "context_windows": {
            "kimi-k3": 1_048_576,
            "kimi-k2.7-code": 262_144,
            "kimi-k2.7-code-highspeed": 262_144,
            "kimi-k2.6": 262_144,
            # 历史别名（老配置仍可解析窗口；价格按模型指纹归一到现行键）
            "moonshot-v1-8k": 8_192,
            "moonshot-v1-32k": 32_768,
            "moonshot-v1-128k": 131_072,
            "kimi-k2-0711-preview": 262_144,
        },
        # 支持原生推理的模型（K2/K3 系列）
        "reasoning_models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed",
                             "kimi-k2.6", "kimi-k2-0711-preview", "kimi-k2-turbo-preview"],
    },
    "qwen": {
        "name": "通义千问 (DashScope)",
        "kind": "openai",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.7-plus",
        "models": ["qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash", "qwen-flash"],
        "env_key": "DASHSCOPE_API_KEY",
        "context_window": 131_072,
        "context_windows": {
            "qwen3.8-max": 1_000_000,
            "qwen3.7-plus": 1_000_000,
            "qwen3.8-flash": 1_000_000,
            "qwen-flash": 1_000_000,
            # 历史别名（老配置仍可解析窗口）
            "qwen-plus": 131_072,
            "qwen-max": 131_072,
            "qwen-turbo": 131_072,
            "qwen-long": 1_000_000,
        },
        # 支持 thinking/reasoning 的模型（Qwen3 系列）
        "reasoning_models": ["qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash",
                             "qwen3-max", "qwen3-plus", "qwen3-235b-a22b",
                             "qwq-plus", "qwq-32b"],
    },
    "glm": {
        "name": "智谱 GLM",
        "kind": "openai",
        # 注意：智谱定价页是 JS SPA（无静态文本可抓），models.dev 也没有第一方
        # 报价段，因此 GLM 的价格只能取 models.dev 的官方渠道商聚合价（粗略），
        # 或用设置里的 pricing_overrides 手填官方价。
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-5.3",
        "models": ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-4.6"],
        "env_key": "ZHIPUAI_API_KEY",
        "context_window": 131_072,
        "context_windows": {
            "glm-5.3": 1_000_000,
            "glm-5.3-flash": 1_000_000,
            "glm-5.2": 1_000_000,
            "glm-4.6": 200_000,
            # 历史别名（老配置仍可解析窗口）
            "glm-4-plus": 131_072,
            "glm-4-flash": 131_072,
            "glm-4-air": 131_072,
            "glm-4-long": 1_000_000,
        },
        # 支持 thinking 的模型（GLM-4.5 及以后）
        "reasoning_models": ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-4.6",
                             "glm-4.5", "glm-4.5-air"],
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "kind": "anthropic",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-5",
        "models": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5",
                   "claude-fable-5-1"],
        "env_key": "ANTHROPIC_API_KEY",
        "context_window": 200_000,
        "context_windows": {
            "claude-sonnet-5": 1_000_000,
            "claude-opus-5": 1_000_000,
            "claude-haiku-4-5": 200_000,
            "claude-fable-5-1": 1_000_000,
            # 历史别名（老配置仍可解析窗口与价格）
            "claude-sonnet-4-20250514": 200_000,
            "claude-opus-4-20250514": 200_000,
            "claude-3-7-sonnet-20250219": 200_000,
            "claude-3-5-sonnet-20241022": 200_000,
        },
        # Claude 3.7 起全系支持扩展思考（extended thinking）
        "reasoning_models": [
            "claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5",
            "claude-fable-5-1",
            "claude-sonnet-4-20250514", "claude-opus-4-20250514",
            "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022",
        ],
    },
    "custom": {
        "name": "自定义 (OpenAI 兼容)",
        "kind": "openai",
        "default_base_url": "",
        "default_model": "",
        "models": [],
        "env_key": "",
        "context_window": 128_000,
    },
}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def _is_masked_key(key: str) -> bool:
    """判断是否是脱敏 key（含省略号或全星号），不能作为真实 key 使用。"""
    return "…" in key or key == "****"


class LLMRegistry:
    """管理多供应商配置并构建适配器。

    配置结构（config.json 的 "llm" 段）:
    {
      "active": "deepseek",
      "providers": {
        "deepseek": {"api_key": "...", "base_url": "...", "model": "...", "temperature": 0.2},
        ...
      }
    }
    """

    def __init__(
        self,
        llm_config: Optional[Dict[str, Any]] = None,
        config_dir: Optional[str] = None,
    ) -> None:
        self.providers: Dict[str, Dict[str, Any]] = {
            pid: {
                "api_key": "",
                "base_url": meta["default_base_url"],
                "model": meta["default_model"],
                "models": list(meta.get("models", [])),
                "temperature": 0.2,
                "reasoning_effort": "",
            }
            for pid, meta in PROVIDER_META.items()
        }
        self.active = "deepseek"
        self._adapter: Optional[BaseLLMAdapter] = None
        # 模型元数据服务（models.dev 同步 + 内置静态表兜底）
        self.meta_service = None
        if config_dir:
            from .model_meta import ModelMetaService

            self.meta_service = ModelMetaService(
                os.path.join(config_dir, "models.dev.json")
            )
        self._apply_env_defaults()
        if llm_config:
            self.apply_config(llm_config)

    def _apply_env_defaults(self) -> None:
        """从环境变量兜底注入 API Key（仅当前为空时）。

        优先级：配置文件 > CLI --api-key > 环境变量。此前实现是无条件覆盖，
        导致设了 DEEPSEEK_API_KEY 的机器上配置文件里的 key 被环境变量顶掉
        （且 _persist_config 会把环境变量值写回配置文件，污染用户真实 key）。
        """
        for pid, meta in PROVIDER_META.items():
            env_key = meta.get("env_key")
            if not env_key:
                continue
            if self.providers.get(pid, {}).get("api_key"):
                continue  # 已有配置值：配置优先，环境变量只兜底空位
            val = os.environ.get(env_key, "").strip()
            if val:
                self.providers[pid]["api_key"] = val

    # ------------------------------------------------------------ 配置

    def apply_config(self, config: Dict[str, Any]) -> None:
        if config.get("active"):
            self.active = config["active"]
        for pid, settings in (config.get("providers") or {}).items():
            if not isinstance(settings, dict):
                continue
            if pid not in self.providers:
                if not (pid.startswith("custom_") or settings.get("kind") == "openai"):
                    continue
                self.providers[pid] = {
                    "name": settings.get("name") or pid,
                    "api_key": "",
                    "base_url": "",
                    "model": "",
                    "models": [],
                    "temperature": 0.2,
                    "reasoning_effort": "",
                    "custom_headers": {},
                }
            # 跳过脱敏 / 空的 api_key，防止用「sk-c…1f74」这种脱敏值覆盖真实 key
            api_key = settings.get("api_key")
            if api_key is None or api_key == "" or _is_masked_key(api_key):
                settings = {k: v for k, v in settings.items() if k != "api_key"}
            merged = {**self.providers[pid], **{k: v for k, v in settings.items() if v is not None}}
            # 旧配置只有 model；新配置保留多个可选模型并继续用 model 表示当前选择。
            configured_models = merged.get("models")
            if not isinstance(configured_models, list):
                configured_models = []
            configured_models = [str(m).strip() for m in configured_models if str(m).strip()]
            selected = str(merged.get("model") or "").strip()
            if selected and selected not in configured_models:
                configured_models.insert(0, selected)
            merged["models"] = configured_models
            self.providers[pid] = merged
        # 环境变量兜底（配置为空时）
        self._apply_env_defaults()

    def to_config(self, persist_key: bool = False) -> Dict[str, Any]:
        """导出配置。

        persist_key=False（API 返回）：api_key 不回写，仅保留 has_key 标记，避免泄露真实 key。
        persist_key=True（落盘）：写入真实 api_key，保证重启后 key 不丢失。
        """
        providers = {}
        for pid, p in self.providers.items():
            providers[pid] = {
                **({"name": p.get("name", pid)} if pid.startswith("custom_") else {}),
                "api_key": p.get("api_key", "") if persist_key else "",
                "has_key": bool(p.get("api_key")),
                "base_url": p.get("base_url", ""),
                "model": p.get("model", ""),
                "models": p.get("models", []),
                "temperature": p.get("temperature", 0.2),
                "reasoning_effort": p.get("reasoning_effort", ""),
                "context_window": p.get("context_window"),
                "custom_headers": p.get("custom_headers") or {},
            }
        return {"active": self.active, "providers": providers}

    def provider_meta(self) -> List[Dict[str, Any]]:
        out = []
        all_ids = list(PROVIDER_META) + [pid for pid in self.providers if pid not in PROVIDER_META]
        for pid in all_ids:
            meta = PROVIDER_META.get(pid, {
                "name": self.providers.get(pid, {}).get("name", pid),
                "kind": "openai", "default_base_url": "", "models": [],
            })
            p = self.providers.get(pid, {})
            reasoning_models = meta.get("reasoning_models", [])
            current_model = p.get("model", "")
            out.append({
                "id": pid,
                "name": meta["name"],
                "kind": meta["kind"],
                "models": p.get("models") or meta.get("models", []),
                "default_base_url": meta["default_base_url"],
                "has_key": bool(p.get("api_key")),
                "model": current_model,
                "context_window": self.get_context_window(pid, current_model),
                "reasoning_models": list(reasoning_models),
                "reasoning_supported": current_model in reasoning_models,
            })
        return out

    # ------------------------------------------------------------ 上下文窗口

    def get_context_window(self, provider_id: str, model: Optional[str] = None) -> int:
        """解析模型上下文长度（token）。

        优先级：手动覆盖（设置里手填）→ models.dev 缓存 → 内置静态表 → 供应商默认。
        """
        pid = provider_id or self.active
        meta = PROVIDER_META.get(pid, PROVIDER_META["custom"])
        p = self.providers.get(pid, {})
        # ① 手动覆盖（config 里手填，用户优先）
        manual = p.get("context_window")
        if isinstance(manual, int) and manual > 0:
            return manual
        # ② models.dev 缓存（按 provider + 模型 ID 精确匹配）
        if self.meta_service is not None and model:
            remote = self.meta_service.get_context_window(model, provider_id=pid)
            if remote:
                return remote
        # ③ 内置静态表（per-model → provider 默认）
        windows = meta.get("context_windows") or {}
        if model and isinstance(windows.get(model), int):
            return windows[model]
        default = meta.get("context_window")
        if isinstance(default, int) and default > 0:
            return default
        return 128_000

    def get_model_pricing(self, provider_id: str = "", model: Optional[str] = None) -> Optional[Dict[str, float]]:
        """解析模型定价（每 M token）：models.dev per-model 数据，无数据返回 None。"""
        if self.meta_service is None or not model:
            return None
        return self.meta_service.get_pricing(model, provider_id=provider_id or self.active)

    def refresh_models_dev(self, force: bool = False) -> bool:
        """同步 models.dev 元数据（失败静默降级到内置表）。

        force=True：绕过 TTL 挡板强制联网拉取（用户手动同步用）。
        """
        if self.meta_service is None:
            return False
        return self.meta_service.refresh(force=force)

    # ------------------------------------------------------------ 适配器

    def get_active_provider_settings(self) -> Dict[str, Any]:
        return self.providers.get(self.active, {})

    def build_adapter(self, provider_id: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> BaseLLMAdapter:
        pid = provider_id or self.active
        meta = PROVIDER_META.get(pid, {
            "name": self.providers.get(pid, {}).get("name", pid),
            "kind": "openai", "default_base_url": "", "default_model": "",
        })
        settings = {**self.providers.get(pid, {}), **(overrides or {})}
        api_key = settings.get("api_key", "")
        # 兜底：overrides 传入脱敏 key 视为未配置，避免「sk-c…1f74」进入真实请求
        if api_key and _is_masked_key(api_key):
            api_key = ""
        if not api_key:
            raise ValueError(f"供应商「{meta['name']}」未配置 API Key")

        common = dict(
            api_key=api_key,
            base_url=settings.get("base_url") or meta["default_base_url"],
            model=settings.get("model") or meta["default_model"],
            temperature=float(settings.get("temperature", 0.2)),
            provider_id=pid,
            enable_cache=bool(settings.get("enable_cache", True)),
            custom_headers=settings.get("custom_headers") or {},
            reasoning_effort=str(settings.get("reasoning_effort", "") or ""),
        )
        if meta["kind"] == "anthropic":
            return AnthropicAdapter(**common)
        return OpenAICompatAdapter(**common)

    def get_adapter(self) -> BaseLLMAdapter:
        if self._adapter is None:
            self._adapter = self.build_adapter()
        return self._adapter

    def reset_adapter(self) -> None:
        self._adapter = None

    async def test_connection(
        self, provider_id: str, overrides: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, float]:
        try:
            adapter = self.build_adapter(provider_id, overrides)
            return await adapter.test_connection()
        except ValueError as exc:
            return False, str(exc), 0
        except Exception as exc:
            logger.exception("[LLM] 测试连接异常")
            return False, str(exc)[:150], 0
