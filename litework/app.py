# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 lite-work contributors

"""AgentApp 装配层：把内核 / LLM / 工具 / 安全 / 会话组装为可运行的 Agent 应用。"""
from __future__ import annotations

import json
import logging
import os
import re
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
from .tools.agent_tools import MultiAgentPlugin
from .tools.plugin import (
    ASTPlugin,
    CodebasePlugin,
    EditorPlugin,
    FileSystemPlugin,
    GitPlugin,
    OcrPlugin,
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
    # 流式空闲看门狗：连续 N 秒无任何 chunk 视为连接卡死，可重试（可见反馈）
    "llm_idle_timeout": 120,
    "subagent_timeout": 600,
    # LLM 瞬时故障（超时/网络/限流/5xx）自动重试次数
    "llm_retries": 2,
    "auto_approve": False,
    "approval_timeout": 600,
    "context_full_turns": 2,
    "mcp_servers": {},
    # 多智能体 P1/P2 配置（docs/multi-agent-design.md §3）
    "max_parallel_agents": 4,   # 并发活 agent 上限
    "agent_total_limit": 16,    # 单会话累计派生上限
    "agent_max_steps": 12,      # 单个子 agent 默认最大轮数（spawn 可覆盖，上限 agent_max_steps_cap）
    "agent_max_steps_cap": 50,  # spawn max_steps 参数的绝对上限（防失控）
    "agent_spawn_depth": 2,     # 嵌套深度上限（1=子不可再派，2=子可派孙，孙不可再派）
    "agent_message_max_chars": 8000,   # agent 间消息单条长度上限（防上下文爆仓）
    "agent_meeting_rounds": 3,  # 会议模式默认轮次（技能配方参考）
    "agent_ledger_interval": 5, # 进度账本：每完成 N 个工具/回合后检查一次子 agent 状态
    "agent_persist_max": 20,    # 落盘归档上限（会话 metadata subagent_records 保留条数）
    # 治理档位（P3，对齐 Codex MultiAgentMode）：explicit=用户明确要求才派生（默认）；
    # proactive=主动并行委派。注入 spawn_agent 工具描述
    "agent_collab_mode": "explicit",
    # 技能权限（对齐 OpenCode permission.skill）：glob 模式 → allow/deny/ask，
    # 插入序首个命中生效，默认 allow；deny 对 Agent 完全隐藏，ask 使用前需审批
    "skill_permissions": {},
    # Skills zip 导入大小上限（MB）
    "max_zip_size_mb": 20,
    # triggers 匹配模式："substring"（大小写不敏感子串）| "advanced"（词边界+正则）
    "skill_trigger_mode": "substring",
    # 并行工具执行："auto"（只读轮并行/含写类整轮串行）| "always" | "never"
    "parallel_tool_calls": "auto",
    # 定价（每 M token，美元）：仅作 models.dev 无该模型数据时的回退。
    # 默认对齐内置默认供应商 DeepSeek —— deepseek-flash 峰值价：缓存未命中输入
    # $0.3 / 输出 $1.2 / 缓存命中 $0.006（官方定价页）。真实价格优先取 models.dev
    # 的 per-model 数据（同步成功后自动生效）。cache_hit 缺省按 input 的 10% 折算
    # （Anthropic 0.1x 惯例），显式给出则不再折算。
    "pricing": {"input_per_mtok": 0.3, "output_per_mtok": 1.2, "cache_hit_per_mtok": 0.006},
    # 效率机制（v1.6.0，SoL-Pi 存活机制的适配）：
    # observation_pack      大工具结果「先全文后占位符」，obs_recall 分页召回
    # compaction_economics  压缩经济学决策（写入成本+缓存债 vs 剩余轮数收益）
    # reducer_model/provider 证据收据的小模型（opt-in，留空停用）
    "observation_pack": True,
    "compaction_economics": True,
    "reducer_model": "",
    "reducer_provider": "",
    # 聊天区展示折叠阈值（数据不删，仅 UI 折叠）：轮数或消息数任一超限即折叠。
    # 高工具密度会话里 1 轮可含几十个工具卡片，只按轮数阈值会形同虚设，
    # 因此补充消息数维度（v1.6.0）。
    "chat_fold_turns": 500,
    "chat_fold_messages": 600,
}

# 历史默认回退定价（对齐 OpenAI 档，远高于默认供应商 DeepSeek 的真实价：
# 未命中输入约 10 倍、输出约 4 倍、缓存命中约 27 倍）——配置里恰好等于该值
# 时说明用户从未自定义，按新默认迁移，否则「预估成本」会一直偏高一个数量级。
LEGACY_DEFAULT_PRICING: Dict[str, Any] = {"input_per_mtok": 1.6, "output_per_mtok": 4.8}

# deepseek 官方现行模型名与历史别名（旧名仍受理，单按 Flash 计费）
DEEPSEEK_CURRENT_MODEL = "deepseek-flash"
DEEPSEEK_LEGACY_MODEL = "deepseek-v4-flash"

TOOL_NAMES = [
    "read_file", "write_file", "list_dir", "file_tree",
    "search_code", "get_file_outline", "read_focused_symbol",
    "apply_search_replace", "apply_unified_diff",
    "execute_command", "check_command", "git_status", "git_diff", "git_log",
    "git_commit", "git_branch", "review_code", "spawn_sub_agent",
        "webfetch", "webfetch_batch", "load_skill",
    # 办公工具（AGI 通用入口）
    "docx_create", "xlsx_create", "pptx_create", "pdf_create",
    "data_analyze", "chart_make",
    # 读取已有办公文件（调研/参考）
    "docx_read", "xlsx_read", "pptx_read", "pdf_read",
    # OCR 识别（图片/PDF/PPT 内嵌图片文字提取）
    "ocr_image", "ocr_document", "ocr_pptx",
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
        self._local_plugins: Optional[List[Plugin]] = None
        self._shell_plugin: Optional[ShellPlugin] = None
        self.approval_gate = ApprovalGate(
            timeout_seconds=self.config.get("approval_timeout", 600)
        )

        # 会话存储
        self.session_store = SessionStore(os.path.join(self.config_dir, "sessions"))

        # 最近打开的项目（侧边栏「项目」页签：打开/新建后记录，最多保留 20 个）
        self.recent_projects_path = os.path.join(self.config_dir, "recent_projects.json")

        # 内置技能安装到用户级 ~/.agents/skills/（幂等；Agent 依赖稳定路径
        # 跑技能脚本，仓库/_internal 内置位置换机器/升级后会漂移）。
        # 引擎 node_modules 不拷贝（数百 MB），通过 LITEWORK_SKILLS_SOURCE
        # 指向安装包内置位置，渲染脚本子进程自动继承。
        try:
            from .tools.skills import _builtin_skills_dir, sync_builtin_skills_to_user
            builtin_dir = _builtin_skills_dir()
            if builtin_dir is not None:
                os.environ.setdefault("LITEWORK_SKILLS_SOURCE", str(builtin_dir))
            n = sync_builtin_skills_to_user()
            if n:
                logger.info("[App] 已安装/升级 %d 个内置技能到 ~/.agents/skills/", n)
        except Exception:
            logger.debug("[App] 内置技能同步失败（不影响启动）", exc_info=True)

        # 图表渲染引擎预装（@plantuml/core / @resvg/resvg-js / mmdc）：
        # 后台线程跑 ~/.agents/skills/diagram-to-office/render_diagram.py --install，
        # 幂等（已装即跳过）、不阻塞启动；离线/失败静默（渲染走内置兜底链）
        self._start_engine_preinstall()

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

        # 多智能体：会话级 AgentManager 注册表（懒创建，spawn 时首次建立）
        from .orchestration.agent_manager import SessionAgentManager
        self.agent_managers: Dict[str, SessionAgentManager] = {}

    def agent_manager(self, session_id: str, create: bool = True) -> Optional["SessionAgentManager"]:
        """取（或建）会话级多 Agent 管理器。create=False 时仅查询（AgentLoop 注入用）。"""
        m = self.agent_managers.get(session_id)
        if m is None and create:
            from .orchestration.agent_manager import SessionAgentManager
            m = self.agent_managers[session_id] = SessionAgentManager(self, session_id)
        return m

    def background_agent_count(self) -> int:
        """全部会话中仍在运行的后台子 Agent 数（MCP 热重载 / 工作区切换守卫用）。

        后台子 Agent 生命周期超出主任务（spawn_agent 异步派生），其 registry
        捕获了装配时的 MCPClient——reload 会 close 这些连接、切工作区会移走
        其 worktree 基准，必须等它们结束。
        """
        return sum(
            1
            for manager in self.agent_managers.values()
            for record in manager.agents.values()
            if getattr(record, "status", "") == "running"
        )

    def collab_modes(self) -> List["Plugin"]:
        """协作模式插件：litework/builtin_plugins/ 内置 v1.0.0，
        ~/.lite-work/plugins/ 同名包覆盖（社区更新机制）。"""
        from .tools.plugin_loader import load_plugins, load_collab_builtin

        if self._local_plugins is None:
            self._local_plugins = load_plugins(self.config_dir)
        from .orchestration.collab_policy import CollabModePlugin

        local = [
            p for p in self._local_plugins
            if isinstance(p, CollabModePlugin) and getattr(p, "mode_name", "")
        ]
        for p in local:
            p._is_local_override = True
        local_names = {p.name for p in local}
        return [m for m in load_collab_builtin() if m.name not in local_names] + local

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
            migrated = False
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.config.update({k: v for k, v in loaded.items() if v is not None})
                if self.config.get("max_steps") == 25:
                    self.config["max_steps"] = 100
                    migrated = True
                # 兼容旧配置：历史默认定价（1.6/4.8）会把预估成本放大一个数量级
                if self.config.get("pricing") == LEGACY_DEFAULT_PRICING:
                    self.config["pricing"] = dict(DEFAULT_CONFIG["pricing"])
                    migrated = True
                # 兼容旧配置：deepseek 旧模型名 → 官方现行名
                if self._migrate_deepseek_model_name():
                    migrated = True
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
                # 迁移结果落盘，避免每次启动重复迁移。必须在 apply_config 之后调用：
                # _persist_config 会用注册表状态重写 llm 段，过早调用会把用户真实
                # API Key 覆盖成注册表默认值。
                if migrated:
                    self._persist_config()
                    logger.info("[App] 配置已迁移并落盘: %s", self.config_path)
            except Exception:
                logger.exception("[App] 配置文件解析失败，使用默认配置")
        else:
            self._write_default_config()
        # 扫描 agents 目录下的自定义 agent 文件
        self.agent_registry.load_dir(os.path.join(self.config_dir, "agents"))

    def _migrate_deepseek_model_name(self) -> bool:
        """把 deepseek 供应商配置里的旧模型名迁到官方现行名。

        官方改名 deepseek-v4-flash → deepseek-flash（旧名仍受理、按 Flash 计费），
        模型列表与 models.dev 定价查询都以现行名为准。只改内置 deepseek 供应商：
        用户自定义中转可能刻意用旧名对接上游，不能替他们改。
        """
        llm_cfg = self.config.get("llm")
        providers = llm_cfg.get("providers") if isinstance(llm_cfg, dict) else None
        entry = providers.get("deepseek") if isinstance(providers, dict) else None
        if not isinstance(entry, dict):
            return False
        changed = False
        if entry.get("model") == DEEPSEEK_LEGACY_MODEL:
            entry["model"] = DEEPSEEK_CURRENT_MODEL
            changed = True
        models = entry.get("models")
        if isinstance(models, list) and DEEPSEEK_LEGACY_MODEL in models:
            renamed: List[Any] = []
            for name in models:
                cur = DEEPSEEK_CURRENT_MODEL if name == DEEPSEEK_LEGACY_MODEL else name
                if cur not in renamed:  # 重名去重（保序）
                    renamed.append(cur)
            entry["models"] = renamed
            changed = True
        return changed

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

    # ------------------------------------------------------------ 图表引擎预装

    ENGINE_PREINSTALL_LOCK = "engine-preinstall.lock"
    ENGINE_PREINSTALL_STALE_S = 900  # 锁超过 15 分钟视为残留（npm 卡死防护）

    def _start_engine_preinstall(self) -> None:
        """后台预装图表渲染引擎（daemon 线程，不阻塞启动）。

        多窗口/多 Core 并发启动时用文件锁防 npm install 冲突；
        离线或 npm 不可用时静默失败——渲染管线有内置兜底链
        （qlmanage / matplotlib），不影响功能可用性。
        """
        import os as _os
        import threading

        # 测试环境开关：并发创建大量 AgentApp 会拖起一堆 npm install，
        # 测试套件设置 LITEWORK_SKIP_ENGINE_PREINSTALL=1 跳过
        if _os.environ.get("LITEWORK_SKIP_ENGINE_PREINSTALL") == "1":
            return
        threading.Thread(
            target=self._engine_preinstall_worker, name="engine-preinstall", daemon=True
        ).start()

    def _engine_preinstall_worker(self) -> None:
        import subprocess
        import time as _time
        import shutil as _shutil

        script = os.path.join(os.path.expanduser("~"), ".agents", "skills",
                              "diagram-to-office", "render_diagram.py")
        if not os.path.isfile(script):
            # 技能尚未同步（同步失败等）——静默跳过
            return
        if _shutil.which("python3") is None and _shutil.which("python") is None:
            return

        # 文件锁：并发启动防护；残留锁超时自动接管。
        # 全局锁放在 ~/.agents/skills/ 下（跨实例/跨 config_dir 共享，真正防并发）
        lock = os.path.join(os.path.expanduser("~"), ".agents", "skills",
                            self.ENGINE_PREINSTALL_LOCK)
        now = _time.time()
        try:
            if os.path.isfile(lock):
                age = now - os.path.getmtime(lock)
                if age < self.ENGINE_PREINSTALL_STALE_S:
                    logger.debug("[App] 引擎预装锁存在（%.0fs 前），跳过", age)
                    return
                logger.warning("[App] 引擎预装锁已残留 %.0fs，接管重装", age)
            with open(lock, "w", encoding="utf-8") as f:
                f.write(str(now))
        except OSError:
            return  # config_dir 不可写——放弃（不影响启动）

        try:
            python = _shutil.which("python3") or _shutil.which("python")
            r = subprocess.run(
                [python, script, "--install"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=900,
            )
            if r.returncode == 0:
                logger.info("[App] 图表引擎预装完成（plantuml/mermaid 离线渲染就绪）")
            else:
                logger.warning("[App] 图表引擎预装未完全成功（离线或网络受限，"
                               "渲染走内置兜底）：%s", (r.stderr or r.stdout).strip()[:200])
        except Exception as exc:
            logger.debug("[App] 图表引擎预装异常（忽略）：%s", exc)

        # matplotlib 字体缓存预热：首次建缓存要数秒且会卡住首次图表渲染，
        # 启动后台顺带建好（兜底渲染首次使用即全速）
        try:
            import matplotlib as _mpl
            _mpl.use("Agg")
            import matplotlib.font_manager as _fm
            _fm.findfont(_fm.FontProperties(family="sans-serif"))
        except Exception:
            pass
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

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

    def resolve_pricing(self, provider_id: str, model: str) -> Dict[str, float]:
        """解析计费单价（每 M token，美元）：models.dev per-model 优先，回退 config 定价。

        回退价（设置 → 定价）是全局标量，对未收录模型/自定义实例只能给粗略估计；
        统一在此解析，保证面板展示的单价与实际成本估算用的是同一份。
        """
        pricing = dict(self.config.get("pricing") or DEFAULT_CONFIG["pricing"])
        model_pricing = self.llm_registry.get_model_pricing(provider_id, model)
        if model_pricing:
            pricing.update(model_pricing)
        return pricing

    def refresh_model_meta(self) -> bool:
        """同步 models.dev 模型元数据（启动时调用，失败静默降级）。"""
        return self.llm_registry.refresh_models_dev()

    def model_meta_status(self) -> Dict[str, Any]:
        """models.dev 元数据缓存状态（设置页「模型元数据」区展示）。

        同步失败时整个进程都会用回退价估算成本，因此把缓存是否可用、索引了
        多少模型、缓存文件年龄暴露出来，让用户能判断要不要手动同步。
        """
        svc = getattr(self.llm_registry, "meta_service", None)
        if svc is None:
            return {"cached": False, "models": 0, "age_seconds": None}
        return svc.status()

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

    def _builtin_plugins(self, workspace_override: Optional[str] = None) -> List[Plugin]:
        """内置 Cordis 插件清单（内核装配与元信息读取共用）。

        workspace 可能为 None（桌面态未打开项目）：此时用家目录兜底——
        仅构造实例读取工具定义，不会真正执行文件操作。
        workspace_override（P3 worktree 隔离）：以指定目录为工作区构建全新
        插件实例（不复用缓存——缓存实例绑定主工作区，重用会破坏隔离）。
        """
        ws = workspace_override or self.workspace or os.path.expanduser("~")
        shell_timeout = float(self.config.get("tool_timeout", 120))
        if workspace_override is None and self._shell_plugin is not None:
            shell = self._shell_plugin
        else:
            shell = ShellPlugin(ws, timeout_seconds=shell_timeout)
            if workspace_override is None:
                self._shell_plugin = shell
        return [
            FileSystemPlugin(ws),
            CodebasePlugin(ws),
            ASTPlugin(ws),
            EditorPlugin(ws),
            shell,
            GitPlugin(ws),
            ReviewPlugin(ws),
            WebFetchPlugin(cache_dir=os.path.join(self.config_dir, "webfetch_cache")),
            OfficePlugin(ws),
            OcrPlugin(ws),
            SubAgentPlugin(self),
            MultiAgentPlugin(self),
            SkillsPlugin(ws),
            self.todo_plugin,
            QuestionPlugin(self.question_gate),
        ]

    def tool_plugins(self, workspace: Optional[str] = None) -> List[Plugin]:
        """Cordis 风格工具插件清单（空间解耦：工具能力全部由插件提供）。

        优先级：用户插件 > 内置插件。同名用户插件会跳过内置版（用户可单独
        更新/覆盖内置插件），卸载用户版后自动回退内置版。
        本地插件只加载一次（缓存），避免每次建内核重复 import。
        workspace（P3）：worktree 隔离时以指定目录构建内置插件实例。
        """
        if self._local_plugins is None:
            from .tools.plugin_loader import load_plugins

            self._local_plugins = load_plugins(self.config_dir)
        local_names = {p.name for p in self._local_plugins}
        plugins: List[Plugin] = [
            p for p in self._builtin_plugins(workspace) if p.name not in local_names]
        plugins.extend(self._local_plugins)
        return plugins

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
        workspace: Optional[str] = None,
    ) -> ToolRegistry:
        """通过 Cordis 内核组装工具集：插件安装到引导内核，注册进 tools 服务。

        与 AgentLoop 一致：内核持有 tools 服务（ToolRegistry），
        插件通过依赖注入获取并注册自己的工具。
        """
        kernel = Kernel(session_id="tool-bootstrap")
        registry = ToolRegistry()
        kernel.register_service(TOOLS_SERVICE, registry)
        kernel.register_service(TOOL_FILTER_SERVICE, self._tool_filter(allowed, exclude, permissions))
        # app 服务必须先于插件 install 注册：社区插件（无参构造）依赖
        # install(kernel) 时从 app 服务捕获 workspace，晚注册会拿到 None
        # → OfficeTools(None) 兜底到家目录（.outputs 落到 ~/.outputs 的根因）
        kernel.register_service("app", self)
        for plugin in self.tool_plugins(workspace):
            kernel.use(plugin)
        self.mcp_manager.register_tools(registry, allowed=allowed, exclude=exclude)
        return registry

    # ------------------------------------------------------------ 内核

    def create_kernel(self, session_id: str, registry: Optional[ToolRegistry] = None,
                      security_workspace: Optional[str] = None) -> Kernel:
        """Cordis 内核装配：工具插件 + 安全插件全部挂载，服务进入依赖注入容器。

        - registry 为 None：挂载全量工具（默认内核）。
        - registry 已传入（build_registry 按 Agent 裁剪后的）：直接挂为 tools 服务，
          不再重复安装工具插件——否则插件会把被裁剪掉的工具重新注册进
          registry（无 tool_filter 服务时插件全量注册），plan 只读模式失效。
        """
        kernel = Kernel(session_id)
        # app 服务先于插件 install 注册（时序约定，见 build_registry 同名注释）
        kernel.register_service("app", self)
        if registry is not None:
            kernel.register_service(TOOLS_SERVICE, registry)
            if registry.has("spawn_sub_agent"):
                from .tools.sub_agent import make_sub_agent_handler

                registry.set_handler(
                    "spawn_sub_agent", make_sub_agent_handler(self, kernel.events)
                )
            # 多 Agent 工具（P1/P2）：handler 绑定本 kernel（fork_context 消息源 + events 转发）
            if registry.has("spawn_agent"):
                from .tools.agent_tools import make_agent_tool_handlers

                for tool_name, handler in make_agent_tool_handlers(self, kernel.events, kernel).items():
                    if registry.has(tool_name):
                        registry.set_handler(tool_name, handler)
            if registry.has("ask_user"):
                from .tools.ask import make_ask_user_handler

                registry.set_handler(
                    "ask_user", make_ask_user_handler(self.question_gate, kernel.events)
                )
        else:
            kernel.register_service(TOOLS_SERVICE, ToolRegistry())
            for plugin in self.tool_plugins():
                kernel.use(plugin)
        # security_workspace（P3 worktree 隔离）：子 Agent 在独立工作树执行时，
        # 安全门的「项目内」边界以工作树为准（否则写入全被当项目外拦截）
        kernel.use(SecurityPlugin(self.guard, self.approval_gate,
                                  security_workspace or self.workspace,
                                  skill_perm_resolver=self.skill_permission_rules))
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

    def skills_import(self, source: str, scope: str = "workspace", name: Optional[str] = None,
                      overwrite: bool = False) -> List[Dict[str, Any]]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).import_skill(source, scope, name, overwrite)

    def skills_import_zip(self, data: bytes, scope: str = "workspace", name: Optional[str] = None,
                          overwrite: bool = False) -> List[Dict[str, Any]]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).import_zip_bytes(data, scope, name, overwrite)

    def skills_delete(self, name: str, scope: str) -> Dict[str, Any]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).delete_skill(name, scope)

    def skills_update(self, name: str, description: str, scope: str) -> Dict[str, Any]:
        from .tools.skills import SkillsTools
        return SkillsTools(self.workspace).update_skill(name, description, scope)

    # ------------------------------------------------------------ 后台命令（Web/API 薄封装）

    def background_tasks(self) -> List[Dict[str, Any]]:
        """列出所有后台命令（execute_command background=true 启动的）。"""
        if self._shell_plugin is None:
            return []
        return self._shell_plugin._tools.list_background()

    def kill_background_task(self, task_id: str) -> bool:
        """杀掉指定后台命令。"""
        if self._shell_plugin is None:
            return False
        return self._shell_plugin._tools.kill_background(task_id)

    # ------------------------------------------------------------ Plugins 管理（Web/API 薄封装）

    def plugins_list(self) -> List[Dict[str, Any]]:
        from .tools.plugin_loader import list_plugins
        return list_plugins(self.config_dir)

    def _invalidate_plugin_cache(self) -> None:
        """清空已加载插件实例缓存：安装/删除/覆盖后立即生效。

        每个任务（TaskManager.start）都会重新装配 kernel + registry，
        缓存失效后无需重启 Core，下一条消息即可用上新工具集。
        """
        self._local_plugins = None

    def plugins_import_zip(self, data: bytes, name: Optional[str] = None,
                           overwrite: bool = False) -> List[Dict[str, Any]]:
        from .tools.plugin_loader import import_zip_bytes, record_installed

        results = import_zip_bytes(self.config_dir, data, name, overwrite)
        # zip 上传无更新源 URL，记录占位来源以保持列表一致（版本从插件自身读取）
        for r in results:
            record_installed(self.config_dir, r["name"], "", "zip-upload")
        self._invalidate_plugin_cache()
        return results

    def plugins_import(self, source: str, name: Optional[str] = None,
                       overwrite: bool = False) -> List[Dict[str, Any]]:
        from .tools.plugin_loader import import_source
        results = import_source(self.config_dir, source, name, overwrite)
        self._invalidate_plugin_cache()
        return results

    def plugins_delete(self, name: str) -> Dict[str, Any]:
        from .tools.plugin_loader import delete_plugin
        result = delete_plugin(self.config_dir, name)
        self._invalidate_plugin_cache()
        return result

    def plugins_builtin(self) -> List[Dict[str, Any]]:
        """列出内置插件元信息（name/description/tools/version/是否被用户版覆盖）。"""
        from . import __version__ as _app_version

        if self._local_plugins is None:
            from .tools.plugin_loader import load_plugins

            self._local_plugins = load_plugins(self.config_dir)
        local_names = {p.name for p in self._local_plugins}

        results: List[Dict[str, Any]] = []
        for p in self._builtin_plugins():
            tools: List[str] = []
            try:
                tools = [t.name for t in p.get_tools()]
            except Exception:
                logger.debug("[App] 读取插件 %s 工具列表失败", p.name, exc_info=True)
            results.append({
                "name": p.name,
                "description": getattr(p, "description", ""),
                "tools": sorted(set(tools)),
                "version": getattr(p, "version", "") or _app_version,
                "overridden": p.name in local_names,
                "builtin": True,
                "kind": "tool",
            })
        # 内置协作模式：与工具插件同一社区更新机制，无注册工具
        from .tools.plugin_loader import load_collab_builtin

        for m in load_collab_builtin():
            results.append({
                "name": m.name,
                "description": getattr(m, "description", ""),
                "tools": [],
                "version": getattr(m, "version", "") or "1.0.0",
                "overridden": m.name in local_names,
                "builtin": True,
                "kind": "collab",
            })
        return results

    def plugins_community(self, url: str = "") -> Dict[str, Any]:
        """拉取社区仓库 manifest.json。"""
        from .tools.plugin_loader import fetch_community_manifest, DEFAULT_COMMUNITY_URL

        return fetch_community_manifest(url or DEFAULT_COMMUNITY_URL)

    def plugins_install(self, source: str, name: Optional[str] = None,
                        overwrite: bool = False, version: Optional[str] = None) -> List[Dict[str, Any]]:
        """安装/更新插件，记录版本与来源。"""
        from .tools.plugin_loader import install_from_source

        results = install_from_source(self.config_dir, source, name=name, overwrite=overwrite, version=version)
        self._invalidate_plugin_cache()
        return results

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

    def agents_available_tools(self) -> Dict[str, Any]:
        """返回全部可用工具（含内置+插件+MCP）+ 各 agent 当前的工具白名单。

        供设置界面「Agent」页配置每个 agent 可用哪些工具（新增/调整）：
        - tools: 去重后的全部工具列表 [{name, description, source}]
        - agents: 每个 agent 的 tools 白名单（None 表示全量）
        """
        registry = self.build_registry()
        tools = []
        seen = set()
        for t in registry.get_tools():
            if t.name in seen:
                continue
            seen.add(t.name)
            tools.append({"name": t.name, "description": t.description})
        tools.sort(key=lambda t: t["name"])
        return {
            "tools": tools,
            "agents": {aid: p.to_dict() for aid, p in self.agent_registry.all().items()},
        }

    def agent_save(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """保存（新建或更新）一个 Agent。内置 agent 仅允许更新 tools/permissions，不允许改 id。

        返回保存后的 agent 描述；同步落盘到 ~/.lite-work/agents/{id}.json。
        """
        from .core.agent_profile import AgentProfile

        agent_id = str(profile_data.get("id") or "").strip()
        if not agent_id:
            raise ValueError("agent id 不能为空")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", agent_id):
            raise ValueError("agent id 仅支持字母/数字/._-")

        existing = None
        try:
            existing = self.agent_registry.get(agent_id)
        except KeyError:
            pass
        is_builtin = existing is not None and existing.id in ("build", "plan", "office", "research")

        if is_builtin:
            # 内置 agent：只允许覆盖 tools / permissions / icon
            # （prompt/人格跟随主程序发版，图标是用户偏好允许自定义）
            tools = profile_data.get("tools")
            permissions = profile_data.get("permissions")
            if "tools" in profile_data:
                existing.tools = tools if isinstance(tools, list) else None
            if "icon" in profile_data:
                existing.icon = str(profile_data.get("icon") or "")
            if permissions is not None:
                existing.permissions = {str(k): str(v) for k, v in permissions.items()
                                        if v in ("allow", "deny", "ask")}
            self.agent_registry.register(existing)
            # 内置覆盖只落盘 tools/permissions：prompt 等字段不冻结，
            # 应用升级后内置默认人格自动跟进
            return self._persist_agent(existing, minimal=True)

        # 自定义 agent：完整新建/更新
        profile = AgentProfile.from_dict(profile_data)
        profile.id = agent_id
        if not profile.system_prompt and not profile.description:
            raise ValueError("自定义 agent 至少需要描述或 system prompt")
        self.agent_registry.register(profile)
        return self._persist_agent(profile)

    def _persist_agent(self, profile, minimal: bool = False) -> Dict[str, Any]:
        """把 agent 落盘到 ~/.lite-work/agents/{id}.json。

        minimal=True（内置 agent 覆盖）：只写 id/tools/permissions，加载时
        其余字段与内置默认合并（register 内处理）。
        """
        agents_dir = os.path.join(self.config_dir, "agents")
        os.makedirs(agents_dir, exist_ok=True)
        path = os.path.join(agents_dir, f"{profile.id}.json")
        if minimal:
            data = {"id": profile.id, "tools": profile.tools,
                    "permissions": profile.permissions}
            if profile.icon:
                data["icon"] = profile.icon
        else:
            data = profile.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("[App] Agent %s 已保存: %s", profile.id, path)
        return profile.to_dict()

    def agent_delete(self, agent_id: str) -> Dict[str, Any]:
        """删除一个 agent。

        - 自定义 agent：彻底删除（注册表 + 磁盘文件）
        - 内置 agent：删除用户覆盖文件并恢复内置默认（工具白名单等）
        """
        try:
            self.agent_registry.get(agent_id)
        except KeyError:
            raise ValueError(f"未知 agent: {agent_id}")
        path = os.path.join(self.config_dir, "agents", f"{agent_id}.json")
        if agent_id in ("build", "plan", "office", "research"):
            if os.path.isfile(path):
                os.remove(path)
            # 重新注册内置默认（丢弃运行期修改）
            from .core.agent_profile import (
                default_build_agent, default_office_agent,
                default_plan_agent, default_research_agent,
            )
            defaults = {"build": default_build_agent, "plan": default_plan_agent,
                        "office": default_office_agent, "research": default_research_agent}
            self.agent_registry.register(defaults[agent_id]())
            return {"ok": True, "id": agent_id, "reset": True}
        self.agent_registry.delete(agent_id)
        if os.path.isfile(path):
            os.remove(path)
        return {"ok": True, "id": agent_id}

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
        pricing = self.resolve_pricing(
            getattr(adapter, "provider_id", None) or self.llm_registry.active,
            getattr(adapter, "model", None) or "",
        )
        # 证据收据 reducer（opt-in）：配置 reducer_model 才启用（通常配小模型省成本）
        reducer_adapter = None
        reducer_model = self.config.get("reducer_model")
        if reducer_model:
            try:
                reducer_adapter = self.llm_registry.build_adapter(
                    provider_id=self.config.get("reducer_provider") or None,
                    overrides={"model": reducer_model},
                )
            except Exception:
                logger.warning("[App] reducer 适配器构建失败，证据收据机制停用", exc_info=True)
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
            truncation_dir=os.path.join(self.config_dir, "truncations"),
            reducer_adapter=reducer_adapter,
            enable_observation_pack=bool(self.config.get("observation_pack", True)),
            enable_compaction_economics=bool(self.config.get("compaction_economics", True)),
        )
        # 流式空闲看门狗透传（主/证据收据适配器；子 Agent 走适配器默认值）
        _idle = float(self.config.get("llm_idle_timeout", 120))
        for _ad in (adapter, reducer_adapter):
            if _ad is not None and hasattr(_ad, "idle_timeout"):
                _ad.idle_timeout = _idle
        loop.workspace = self.workspace
        # 多 Agent 通知注入源：按 session_id 查询（懒创建的 manager 也能找到）
        loop.agent_manager_factory = lambda sid: self.agent_manager(sid, create=False)
        return loop

    async def close(self) -> None:
        await self.mcp_manager.close()
        await self.close_adapter()
