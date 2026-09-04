"""AgentApp 装配层：把内核 / LLM / 工具 / 安全 / 会话组装为可运行的 Agent 应用。"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .core.agent_loop import AgentLoop
from .core.agent_profile import AgentProfile, AgentRegistry
from .core.context_manager import ContextManager
from .core.kernel import Kernel
from .core.session_store import SessionStore
from .core.types import Plugin
from .llm.base import BaseLLMAdapter
from .llm.registry import LLMRegistry
from .mcp import MCPManager
from .security.approval import ApprovalGate
from .security.guard import SecurityGuard
from .security.plugin import SecurityPlugin
from .security.question import QuestionGate
from .tools.ask import QuestionPlugin
from .tools.plugin import (
    ASTPlugin,
    CodebasePlugin,
    EditorPlugin,
    FileSystemPlugin,
    GitPlugin,
    OfficePlugin,
    ReviewPlugin,
    ShellPlugin,
    SkillsPlugin,
    SubAgentPlugin,
    TOOL_FILTER_SERVICE,
    TOOLS_SERVICE,
    WebFetchPlugin,
)
from .tools.registry import ToolRegistry

logger = logging.getLogger("litework.app")

DEFAULT_CONFIG: Dict[str, Any] = {
    "max_steps": 100,
    "token_budget": 48000,
    "tool_timeout": 120,
    "llm_timeout": 300,
    # LLM 瞬时故障（超时/网络/限流/5xx）自动重试次数
    "llm_retries": 2,
    "auto_approve": False,
    "approval_timeout": 600,
    "context_full_turns": 2,
    "mcp_servers": {},
    # 技能权限（对齐 OpenCode permission.skill）：glob 模式 → allow/deny/ask，
    # 插入序首个命中生效，默认 allow；deny 对 Agent 完全隐藏，ask 使用前需审批
    "skill_permissions": {},
    # Skills zip 导入大小上限（MB）
    "max_zip_size_mb": 20,
    # triggers 匹配模式："substring"（大小写不敏感子串）| "advanced"（词边界+正则）
    "skill_trigger_mode": "substring",
    # 并行工具执行："auto"（只读轮并行/含写类整轮串行）| "always" | "never"
    "parallel_tool_calls": "auto",
    # 定价（每 M token，美元）：仅作 models.dev 无该模型数据时的回退；
    # cache_hit 缺省按 input 的 10% 折算（Anthropic 0.1x 惯例），真实价格优先取 models.dev
    "pricing": {"input_per_mtok": 1.6, "output_per_mtok": 4.8},
}

TOOL_NAMES = [
    "read_file", "write_file", "list_dir", "file_tree",
    "search_code", "get_file_outline", "read_focused_symbol",
    "apply_search_replace", "apply_unified_diff",
    "execute_command", "git_status", "git_diff", "git_log",
    "git_commit", "git_branch", "review_code", "spawn_sub_agent",
        "webfetch", "webfetch_batch", "load_skill",
    # 办公工具（GAI 通用入口）
    "docx_create", "xlsx_create", "pptx_create", "pdf_create",
    "data_analyze", "chart_make",
]


class AgentApp:
    def __init__(
        self,
        workspace: Optional[str] = None,
        config_dir: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        # Desktop application starts without a project. Keep this as an explicit
        # state instead of silently treating the application's cwd as a workspace.
        self.workspace = os.path.abspath(workspace) if workspace else None
        if self.workspace:
            os.makedirs(self.workspace, exist_ok=True)

        self.config_dir = os.path.abspath(os.path.expanduser(config_dir or "~/.lite-work"))
        self.config_path = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)

        self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)

        # 安全组件（先于配置加载，供默认配置落盘引用）
        self.guard = SecurityGuard()
        self.approval_gate = ApprovalGate(timeout_seconds=600)
        self.question_gate = QuestionGate(timeout_seconds=600)
        self.llm_registry = LLMRegistry(config_dir=self.config_dir)
        self.agent_registry = AgentRegistry()
        self._context_session_stats: Dict[str, Dict[str, Any]] = {}
        # 任务内统计的差分基线（context:stats 每轮推送全量，需减去上次快照才是增量）
        self._last_task_snapshot: Dict[str, Dict[str, Any]] = {}
        self._load_config()
        self.mcp_manager = MCPManager(self.config.get("mcp_servers") or {})
        # 任务 TODO 看板（todo_write 工具 + todo:updated 事件 + 看板持久化）
        from .tools.todos import TodoPlugin
        self.todo_plugin = TodoPlugin(
            storage_dir=os.path.join(self.config_dir, "todo_boards"))
        self.approval_gate = ApprovalGate(
            timeout_seconds=self.config.get("approval_timeout", 600)
        )

        # 会话存储
        self.session_store = SessionStore(os.path.join(self.config_dir, "sessions"))

        # 最近打开的项目（侧边栏「项目」页签：打开/新建后记录，最多保留 20 个）
        self.recent_projects_path = os.path.join(self.config_dir, "recent_projects.json")

        # 兼容旧配置：base_url/model 回填
        if base_url and not self.llm_registry.get_active_provider_settings().get("base_url"):
            self.llm_registry.providers["deepseek"]["base_url"] = base_url
        if model and not self.llm_registry.get_active_provider_settings().get("model"):
            self.llm_registry.providers["deepseek"]["model"] = model
        # 环境变量/key 回填
        self._apply_env_api_key(api_key)

        # 子 Agent 运行器（延迟绑定）
        from .orchestration.sub_agent import SubAgentRunner
        self.sub_agent_runner = SubAgentRunner(self)

    def _apply_env_api_key(self, cli_key: Optional[str] = None) -> None:
        """CLI 传入的 --api-key 回填到所有未配置的供应商。"""
        if cli_key:
            for pid in self.llm_registry.providers:
                p = self.llm_registry.providers[pid]
                if not p.get("api_key"):
                    p["api_key"] = cli_key
        # 确保环境变量中的 key 也注入（registry 已处理，但以防配置被后续覆盖）
        self.llm_registry._apply_env_defaults()

    # ------------------------------------------------------------ 配置

    def _load_config(self) -> None:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.config.update({k: v for k, v in loaded.items() if v is not None})
                if self.config.get("max_steps") == 25:
                    self.config["max_steps"] = 100
                security_cfg = loaded.get("security")
                if isinstance(security_cfg, dict):
                    self.guard.apply_config(security_cfg)
                llm_cfg = loaded.get("llm")
                if isinstance(llm_cfg, dict):
                    self.llm_registry.apply_config(llm_cfg)
                agents_cfg = loaded.get("agents")
                if isinstance(agents_cfg, dict):
                    self.agent_registry.load_config(agents_cfg)
                logger.info("[App] 配置文件已加载: %s", self.config_path)
            except Exception:
                logger.exception("[App] 配置文件解析失败，使用默认配置")
        else:
            self._write_default_config()
        # 扫描 agents 目录下的自定义 agent 文件
        self.agent_registry.load_dir(os.path.join(self.config_dir, "agents"))

    def _write_default_config(self) -> None:
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {**DEFAULT_CONFIG, "llm": self.llm_registry.to_config(),
                     "security": self.guard.to_dict()},
                    f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def save_config(self, updates: Dict[str, Any]) -> None:
        self.config.update({k: v for k, v in updates.items() if v is not None})
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def update_security_rules(self, rules: Dict[str, Any]) -> None:
        """热更新安全规则（动态黑白名单）并落盘。"""
        self.guard.apply_config(rules)
        self.config["security"] = rules
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ 最近项目

    RECENT_PROJECTS_MAX = 20

    def _load_recent_projects_raw(self) -> List[Dict[str, Any]]:
        """读原始最近项目记录（不做过滤/排序）。"""
        try:
            with open(self.recent_projects_path, "r", encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            return []
        return items if isinstance(items, list) else []

    def _save_recent_projects_raw(self, items: List[Dict[str, Any]]) -> None:
        try:
            with open(self.recent_projects_path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def list_recent_projects(self) -> List[Dict[str, Any]]:
        """最近打开的项目列表。目录已删除的项自动剔除。

        排序：pinned 置顶（组内按 pin 时间原顺序），其余按打开时间新→旧。
        """
        items = self._load_recent_projects_raw()
        alive = []
        seen: set = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            path = it.get("path") or ""
            if not path or not os.path.isdir(path):
                continue
            # Windows 大小写不敏感：normcase 归一后去重
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            alive.append({
                "path": path,
                "name": it.get("name") or os.path.basename(path),
                # kind: code=代码仓库 / project=通用项目
                "kind": it.get("kind") if it.get("kind") in ("code", "project") else "project",
                "is_git": bool(it.get("is_git", os.path.isdir(os.path.join(path, ".git")))),
                "pinned": bool(it.get("pinned", False)),
                "opened_at": it.get("opened_at") or "",
            })
        # pinned 前置（稳定排序保持组内原顺序），其余新→旧由插入顺序保证
        alive.sort(key=lambda x: 0 if x["pinned"] else 1)
        return alive[: self.RECENT_PROJECTS_MAX]

    def remember_project(self, path: str, kind: str = "project") -> Dict[str, Any]:
        """记录一次项目打开（去重置顶，超限淘汰最旧）。kind: code/project。

        已 pin 的项目重复打开：保持 pinned 并回到置顶组（不丢 pin 状态）。
        """
        import datetime as _dt
        abs_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(abs_path):
            raise ValueError(f"目录不存在: {abs_path}")
        items = self._load_recent_projects_raw()
        key = os.path.normcase(abs_path)
        # 继承已存在记录的 pinned 状态
        was_pinned = any(
            isinstance(it, dict) and os.path.normcase(os.path.abspath(it.get("path") or "")) == key
            and it.get("pinned")
            for it in items
        )
        items = [it for it in items
                 if not (isinstance(it, dict) and os.path.normcase(os.path.abspath(it.get("path") or "")) == key)]
        items.insert(0, {
            "path": abs_path,
            "name": os.path.basename(abs_path) or abs_path,
            "kind": kind if kind in ("code", "project") else "project",
            "is_git": os.path.isdir(os.path.join(abs_path, ".git")),
            "pinned": was_pinned,
            "opened_at": _dt.datetime.now().isoformat(timespec="seconds"),
        })
        # 淘汰时保护 pinned：超限时优先淘汰未 pin 的最旧项
        pinned_items = [it for it in items if isinstance(it, dict) and it.get("pinned")]
        normal = [it for it in items if not (isinstance(it, dict) and it.get("pinned"))]
        if len(items) > self.RECENT_PROJECTS_MAX:
            normal = normal[: max(0, self.RECENT_PROJECTS_MAX - len(pinned_items))]
        items = self.list_recent_projects_order_hint(pinned_items + normal)
        self._save_recent_projects_raw(items)
        # 返回刚记住的项目（order 后不一定是首位）
        for it in items:
            if isinstance(it, dict) and os.path.normcase(os.path.abspath(it.get("path") or "")) == key:
                return it
        return items[0] if items else {}

    def list_recent_projects_order_hint(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """持久化前的顺序整理：pinned 在前，其余新→旧（insert 顺序即新→旧）。"""
        pinned = [it for it in items if isinstance(it, dict) and it.get("pinned")]
        normal = [it for it in items if not (isinstance(it, dict) and it.get("pinned"))]
        return pinned + normal

    def toggle_project_pin(self, path: str) -> bool:
        """翻转项目的 pinned 状态，返回翻转后的状态。pin 时提到置顶组首位。"""
        items = self._load_recent_projects_raw()
        key = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        target_idx = None
        for i, it in enumerate(items):
            if isinstance(it, dict) and os.path.normcase(os.path.abspath(it.get("path") or "")) == key:
                target_idx = i
                break
        if target_idx is None:
            raise ValueError(f"项目不在最近列表中: {path}")
        it = items[target_idx]
        it["pinned"] = not bool(it.get("pinned", False))
        pinned_now = bool(it["pinned"])
        items.pop(target_idx)
        # pin → 插到置顶组首位；unpin → 放到未 pin 组首位（刚操作过的靠前；
        # opened_at 仅秒级精度，同秒内无法按时间细分）
        if pinned_now:
            insert_at = 0
            for j, other in enumerate(items):
                if not (isinstance(other, dict) and other.get("pinned")):
                    insert_at = j
                    break
            else:
                insert_at = len(items)
            items.insert(insert_at, it)
        else:
            insert_at = 0
            for j, other in enumerate(items):
                if isinstance(other, dict) and other.get("pinned"):
                    insert_at = j + 1
            items.insert(insert_at, it)
        self._save_recent_projects_raw(items)
        return pinned_now

    def forget_project(self, path: str) -> None:
        """从最近列表移除一个项目（不删磁盘文件）。"""
        items = self._load_recent_projects_raw()
        key = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        items = [it for it in items
                 if not (isinstance(it, dict) and os.path.normcase(os.path.abspath(it.get("path") or "")) == key)]
        self._save_recent_projects_raw(items)

    def mcp_status(self) -> Dict[str, Any]:
        return self.mcp_manager.status()

    async def update_mcp_servers(self, servers: Dict[str, Any]) -> Dict[str, Any]:
        """更新 MCP Server 配置：落盘 + 热重连。"""
        self.config["mcp_servers"] = servers
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
        return await self.mcp_manager.reload(servers)

    # ------------------------------------------------------------ LLM

    def _persist_config(self) -> None:
        self.config["llm"] = self.llm_registry.to_config(persist_key=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    async def close_adapter(self) -> None:
        """异步关闭当前 LLM 适配器，供配置热更新时释放连接。"""
        try:
            adapter = self.llm_registry.get_adapter()
            await adapter.close()
        except Exception:
            pass
        self.llm_registry.reset_adapter()

    @property
    def adapter(self) -> BaseLLMAdapter:
        if getattr(self, "_mock_adapter", None) is not None:
            return self._mock_adapter
        try:
            return self.llm_registry.get_adapter()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    def get_llm_config(self) -> Dict[str, Any]:
        return self.llm_registry.to_config()

    def update_llm_config(
        self,
        active: Optional[str] = None,
        providers: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """更新 LLM 配置并重建适配器。providers 中 api_key 为脱敏值时忽略。"""
        if active:
            self.llm_registry.active = active
        if providers:
            for pid, settings in providers.items():
                if pid not in self.llm_registry.providers:
                    if not pid.startswith("custom_"):
                        continue
                    self.llm_registry.providers[pid] = {
                        "name": settings.get("name") or pid, "api_key": "",
                        "base_url": "", "model": "", "models": [], "temperature": 0.2,
                        "reasoning_effort": "", "custom_headers": {},
                    }
                current = self.llm_registry.providers[pid]
                # 跳过脱敏 api_key（含 … 或 **** 视为未修改）
                if settings.get("api_key") and ("…" in settings["api_key"] or settings["api_key"] == "****"):
                    settings.pop("api_key")
                elif "api_key" in settings and not settings["api_key"]:
                    settings.pop("api_key")
                # 前端回传的 has_key 是 UI 展示用标记，不并入 provider 配置
                settings.pop("has_key", None)
                # 空/无效的 context_window（手动覆盖）视为未设置
                if settings.get("context_window") in (None, "", 0):
                    settings.pop("context_window", None)
                # custom_headers 必须是 str:str 字典；非法值忽略，空字典表示明确清空
                if "custom_headers" in settings:
                    raw_headers = settings["custom_headers"]
                    if isinstance(raw_headers, dict):
                        settings["custom_headers"] = {
                            str(k).strip(): str(v).strip()
                            for k, v in raw_headers.items()
                            if isinstance(k, str) and isinstance(v, str)
                            and k.strip() and v.strip()
                        }
                    else:
                        settings.pop("custom_headers")
                merged = {**current, **settings}
                if pid.startswith("custom_"):
                    merged["name"] = str(merged.get("name") or pid).strip()
                self.llm_registry.providers[pid] = merged
        # UI 提交的是完整列表；未提交的自定义实例表示用户删除了它。
        if providers is not None:
            kept = set(providers)
            for pid in list(self.llm_registry.providers):
                if pid.startswith("custom_") and pid not in kept:
                    del self.llm_registry.providers[pid]
        if self.llm_registry.active not in self.llm_registry.providers:
            self.llm_registry.active = "deepseek"
        self.llm_registry.reset_adapter()
        self._persist_config()
        return self.llm_registry.to_config()

    async def test_llm(self, provider_id: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ok, msg, elapsed = await self.llm_registry.test_connection(provider_id, overrides)
        return {"ok": ok, "message": msg, "latency_ms": int(elapsed)}

    def llm_provider_meta(self) -> List[Dict[str, Any]]:
        return self.llm_registry.provider_meta()

    def refresh_model_meta(self) -> bool:
        """同步 models.dev 模型元数据（启动时调用，失败静默降级）。"""
        return self.llm_registry.refresh_models_dev()

    # ------------------------------------------------------------ 上下文统计

    def accumulate_context_stats(self, session_id: str, task_stats: Dict[str, Any]) -> Dict[str, Any]:
        """把任务内统计合并进会话级累计，返回最新会话累计。

        task 段是「任务内累计」口径（AgentLoop 的 stats 字典跨轮持续累加，
        每轮推送的是全量值），而本方法在 context:stats 每轮都会被调用——
        因此必须按「本次全量 − 上次快照」的增量入账，否则多轮任务会把
        会话累计重复放大。任务结束后快照清零，下个任务从 0 重新起算。
        """
        acc = self._context_session_stats.setdefault(session_id, {
            "prompt_tokens": 0, "output_tokens": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "compression_count": 0, "compressed_tokens": 0,
            "tool_calls": 0, "blocked": 0, "cost_estimate": 0.0,
        })
        last = self._last_task_snapshot.get(session_id)
        delta = {}
        for key in ("prompt_tokens", "output_tokens", "cache_hit_tokens",
                    "cache_miss_tokens", "compression_count", "compressed_tokens",
                    "tool_calls", "blocked"):
            cur = int(task_stats.get(key, 0) or 0)
            delta[key] = max(0, cur - int((last or {}).get(key, 0) or 0))
            acc[key] += delta[key]
        cur_cost = float(task_stats.get("cost_estimate", 0) or 0)
        acc["cost_estimate"] = round(acc["cost_estimate"] + max(0.0, cur_cost - float((last or {}).get("cost_estimate", 0) or 0)), 4)
        # 记录本次全量作为下轮差分基线
        self._last_task_snapshot[session_id] = {
            **{k: int(task_stats.get(k, 0) or 0) for k in
               ("prompt_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "compression_count", "compressed_tokens", "tool_calls", "blocked")},
            "cost_estimate": cur_cost,
        }
        hit = acc["cache_hit_tokens"]
        miss = acc["cache_miss_tokens"]
        acc["cache_hit_rate"] = round(hit / (hit + miss), 4) if (hit + miss) > 0 else None
        return dict(acc)

    def get_context_session_stats(self, session_id: str) -> Dict[str, Any]:
        return dict(self._context_session_stats.get(session_id, {}))

    # ------------------------------------------------------------ 工具

    def tool_plugins(self) -> List[Plugin]:
        """Cordis 风格工具插件清单（空间解耦：工具能力全部由插件提供）。"""
        return [
            FileSystemPlugin(self.workspace),
            CodebasePlugin(self.workspace),
            ASTPlugin(self.workspace),
            EditorPlugin(self.workspace),
            ShellPlugin(self.workspace),
            GitPlugin(self.workspace),
            ReviewPlugin(self.workspace),
            WebFetchPlugin(cache_dir=os.path.join(self.config_dir, "webfetch_cache")),
            OfficePlugin(self.workspace),
            SubAgentPlugin(self),
            SkillsPlugin(self.workspace),
            self.todo_plugin,
            QuestionPlugin(self.question_gate),
        ]

    @staticmethod
    def _tool_filter(
        allowed: Optional[List[str]],
        exclude: Optional[List[str]],
        permissions: Optional[Dict[str, str]],
    ):
        """构造工具裁剪策略（Agent 配置：allowed / exclude / deny 权限）。"""

        def allow(name: str) -> bool:
            if allowed is not None and name not in allowed:
                return False
            if exclude and name in exclude:
                return False
            if permissions and permissions.get(name) == "deny":
                return False
            return True

        return allow

    def build_registry(
        self,
        allowed: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        permissions: Optional[Dict[str, str]] = None,
    ) -> ToolRegistry:
        """通过 Cordis 内核组装工具集：插件安装到引导内核，注册进 tools 服务。

        与 AgentLoop 一致：内核持有 tools 服务（ToolRegistry），
        插件通过依赖注入获取并注册自己的工具。
        """
        kernel = Kernel(session_id="tool-bootstrap")
        registry = ToolRegistry()
        kernel.register_service(TOOLS_SERVICE, registry)
        kernel.register_service(TOOL_FILTER_SERVICE, self._tool_filter(allowed, exclude, permissions))
        for plugin in self.tool_plugins():
            kernel.use(plugin)
        self.mcp_manager.register_tools(registry, allowed=allowed, exclude=exclude)
        return registry

    # ------------------------------------------------------------ 内核

    def create_kernel(self, session_id: str, registry: Optional[ToolRegistry] = None) -> Kernel:
        """Cordis 内核装配：工具插件 + 安全插件全部挂载，服务进入依赖注入容器。

        - registry 为 None：挂载全量工具（默认内核）。
        - registry 已传入（build_registry 按 Agent 裁剪后的）：直接挂为 tools 服务，
          不再重复安装工具插件——否则插件会把被裁剪掉的工具重新注册进
          registry（无 tool_filter 服务时插件全量注册），plan 只读模式失效。
        """
        kernel = Kernel(session_id)
        if registry is not None:
            kernel.register_service(TOOLS_SERVICE, registry)
            if registry.has("spawn_sub_agent"):
                from .tools.sub_agent import make_sub_agent_handler

                registry.set_handler(
                    "spawn_sub_agent", make_sub_agent_handler(self, kernel.events)
                )
            if registry.has("ask_user"):
                from .tools.ask import make_ask_user_handler

                registry.set_handler(
                    "ask_user", make_ask_user_handler(self.question_gate, kernel.events)
                )
        else:
            kernel.register_service(TOOLS_SERVICE, ToolRegistry())
            for plugin in self.tool_plugins():
                kernel.use(plugin)
        kernel.use(SecurityPlugin(self.guard, self.approval_gate, self.workspace,
                                  skill_perm_resolver=self.skill_permission_rules))
        kernel.register_service("app", self)
        return kernel

    # ------------------------------------------------------------ Agent

    # ------------------------------------------------------------ Skills 管理（Web/API 薄封装）

    def skills_list(self) -> List[Dict[str, Any]]:
        from .tools.skills import SkillsTools
        skills = SkillsTools(self.workspace).list_skills()
        for s in skills:
            s["permission"] = self.skill_permission(s.get("name") or "")
        return skills

    def skill_permission_rules(self) -> Dict[str, str]:
        """清洗后的技能权限规则（glob 模式 → allow/deny/ask）。"""
        from .security.skill_permissions import normalize_rules
        return normalize_rules(self.config.get("skill_permissions"))

    def skill_permission(self, name: str) -> str:
        """解析单个技能名的动作：allow / deny / ask（默认 allow）。"""
        from .security.skill_permissions import resolve
        return resolve(self.skill_permission_rules(), name)

    def skills_read(self, name: str) -> Optional[str]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).read_skill(name)

    def skills_create(self, name: str, description: str, scope: str = "workspace") -> Dict[str, Any]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).create_skill(name, description, scope)

    def skills_import(self, source: str, scope: str = "workspace", name: Optional[str] = None) -> List[Dict[str, Any]]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).import_skill(source, scope, name)

    def skills_import_zip(self, data: bytes, scope: str = "workspace", name: Optional[str] = None) -> List[Dict[str, Any]]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).import_zip_bytes(data, scope, name)

    def skills_delete(self, name: str, scope: str) -> Dict[str, Any]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).delete_skill(name, scope)

    def commands_list(self) -> List[Dict[str, str]]:
        from .core.commands import build_command_list
        try:
            # deny 的技能不派生命令（对 Agent 隐藏的技能对命令面板也隐藏）
            skills = [s for s in self.skills_list() if s.get("permission") != "deny"]
            return build_command_list(skills)
        except Exception:
            return build_command_list([])

    # ------------------------------------------------------------ /compact 手动压缩

    async def compact_session(self, session_id: str, focus: str = "") -> Dict[str, Any]:
        """手动触发一次会话上下文压缩（策略 B：旧轮次 LLM 摘要化 + 最近 N 轮原样保留）。

        与 AgentLoop 的自动压缩不同：不检查是否超预算——用户显式要求就强制折叠，
        长任务间隙即可主动释放窗口。压缩结果直接落盘回写会话。
        返回 {ok, before_tokens, after_tokens, removed_tokens, turns_compacted,
        keep_turns, summary}；无需压缩时 {ok: False, reason}。
        """
        from .core.context_manager import ContextManager
        from .core.token_counter import TokenCounter
        from .core.types import Message

        snapshot = self.session_store.load(session_id)
        if not snapshot or len(snapshot.messages) < 3:
            return {"ok": False, "reason": "会话为空或内容过少，无需压缩"}

        messages = list(snapshot.messages)
        system = messages[0] if messages[0].role == "system" else None
        body = messages[1:] if system else messages

        # 按轮次强制折叠：保留最近 N 轮完整细节（N 与自动压缩一致，取配置）
        keep_turns = max(1, int(self.config.get("context_full_turns", 2)))
        ranges = ContextManager._turn_ranges(body)
        if len(ranges) <= keep_turns:
            return {"ok": False,
                    "reason": f"会话只有 {len(ranges)} 轮（保留阈值 {keep_turns} 轮），没有可压缩的历史"}
        cut = ranges[-keep_turns][0]
        head, tail = body[:cut], body[cut:]
        head_tokens = sum(TokenCounter.count_message_tokens(m) for m in head)
        if not head or head_tokens <= 0:
            return {"ok": False, "reason": "没有可压缩的历史"}

        summary = await self._summarize_head(head, system, focus)
        if not summary:
            return {"ok": False, "reason": "摘要调用失败（LLM 未返回正文），会话保持不变"}

        compacted = ([system] if system else []) + [
            Message(role="user", content=f"[历史摘要] {summary}")
        ] + tail
        before_tokens = TokenCounter.count_messages_tokens(messages)
        after_tokens = TokenCounter.count_messages_tokens(compacted)
        self.session_store.save(session_id, compacted, metadata=snapshot.metadata)

        # 统计回写：面板立即反映压缩后水位（usage/费用等累计值不动）
        acc = self._context_session_stats.setdefault(session_id, {})
        acc["compression_count"] = int(acc.get("compression_count", 0) or 0) + 1
        acc["compressed_tokens"] = int(acc.get("compressed_tokens", 0) or 0) + max(0, head_tokens - TokenCounter.count_text_tokens(summary))
        acc["last_prompt_tokens"] = after_tokens
        return {
            "ok": True,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "removed_tokens": max(0, before_tokens - after_tokens),
            "turns_compacted": len(ranges) - keep_turns,
            "keep_turns": keep_turns,
            "summary": summary,
        }

    async def _summarize_head(self, head: List[Any], system: Any, focus: str = "") -> Optional[str]:
        """压缩摘要 LLM 调用（无工具、不流式转发），focus 指定摘要保留重点。"""
        from .core.token_counter import TokenCounter
        from .core.types import Message

        body = [m for m in head if m.role != "system"]
        if not body:
            return None
        total_chars = sum(len(m.content or "") for m in body)
        if total_chars > 200_000:
            return None
        instruction = (
            "请将以上全部对话历史压缩为一段精炼的中文摘要，作为后续工作的背景说明：\n"
            "保留已完成的决策与结论、修改过的文件清单、关键发现与未完成的任务，"
            "丢弃过程性细节。直接输出摘要正文，不要任何前缀，不要调用任何工具。"
        )
        if focus:
            instruction += f"\n用户特别要求：摘要重点保留与以下关注点相关的内容：{focus}"
        messages = ([system] if system else []) + body + [
            Message(role="user", content=instruction),
        ]
        try:
            content, _, _ = await self.adapter.chat_stream(messages, [], None)
        except Exception:
            return None
        return (content or "").strip() or None

    def get_agent(self, agent_id: Optional[str] = None) -> AgentProfile:
        """获取 Agent 配置：不传则返回默认 build agent。"""
        return self.agent_registry.get(agent_id or "build")

    def agents_meta(self) -> List[Dict[str, Any]]:
        """供 UI/CLI 列出全部可选 agent。"""
        return [p.to_dict() for p in self.agent_registry.all().values()]

    def create_agent_registry(self, agent_id: str) -> ToolRegistry:
        """按 Agent 配置裁剪工具集（参考 OpenCode：plan 只读、build 全量）。"""
        profile = self.get_agent(agent_id)
        registry = self.build_registry(
            allowed=profile.tools,
            exclude=["spawn_sub_agent"] if agent_id == "plan" else None,
            permissions=profile.permissions,
        )
        return registry

    def create_loop(self, kernel: Kernel, registry: ToolRegistry, agent_id: Optional[str] = None,
                    model_override: Optional[Dict[str, str]] = None,
                    reasoning_effort_override: Optional[str] = None) -> AgentLoop:
        profile = self.get_agent(agent_id)
        adapter = self.adapter
        # reasoning_effort override 语义："off" = 显式关闭（覆盖 provider 默认）；
        # 空串/None = 未指定 → 跟随 provider 配置
        eff_override = reasoning_effort_override
        if eff_override == "off":
            eff_override = ""
        has_eff_override = reasoning_effort_override is not None
        if model_override or profile.model or profile.temperature is not None or has_eff_override:
            overrides: Dict[str, Any] = {}
            provider_id = model_override.get("provider") if model_override else None
            if model_override and model_override.get("model"):
                overrides["model"] = model_override["model"]
            elif profile.model:
                overrides["model"] = profile.model
            if profile.temperature is not None:
                overrides["temperature"] = profile.temperature
            if has_eff_override:
                overrides["reasoning_effort"] = eff_override
            adapter = self.llm_registry.build_adapter(provider_id=provider_id, overrides=overrides)
        # 上下文压缩：策略 B（保留最近 N 轮完整细节），预算 = min(token_budget, 90%×窗口)
        token_budget = int(self.config.get("token_budget", 48000))
        context_manager = ContextManager(
            token_budget,
            keep_recent_full_turns=int(self.config.get("context_full_turns", 2)),
        )
        context_window = self.llm_registry.get_context_window(
            getattr(adapter, "provider_id", None) or self.llm_registry.active,
            getattr(adapter, "model", None),
        )
        # 定价：models.dev per-model（input/output/cache_read 每百万 token）
        # 优先；无该模型数据时回退 config 静态价（pricing 段可配置）
        pricing = dict(self.config.get("pricing") or DEFAULT_CONFIG["pricing"])
        model_pricing = self.llm_registry.get_model_pricing(
            getattr(adapter, "provider_id", None) or self.llm_registry.active,
            getattr(adapter, "model", None),
        )
        if model_pricing:
            pricing.update(model_pricing)
        loop = AgentLoop(
            kernel=kernel,
            adapter=adapter,
            registry=registry,
            session_store=self.session_store,
            context_manager=context_manager,
            max_steps=int(self.config.get("max_steps", 100)),
            tool_timeout=float(self.config.get("tool_timeout", 120)),
            llm_timeout=float(self.config.get("llm_timeout", 300)),
            llm_retries=int(self.config.get("llm_retries", 2)),
            token_budget=token_budget,
            pricing=pricing,
            auto_approve=bool(self.config.get("auto_approve", False)),
            context_window=context_window,
        )
        loop.workspace = self.workspace
        loop.truncation_dir = os.path.join(self.config_dir, "truncations")
        return loop

    async def close(self) -> None:
        await self.mcp_manager.close()
        await self.close_adapter()
